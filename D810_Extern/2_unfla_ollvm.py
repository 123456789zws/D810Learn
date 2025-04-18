from typing import List, Tuple

import ida_bytes
import ida_funcs
import ida_ida
import ida_range
from d810.hexrays_formatters import format_minsn_t
from d810.cfg_utils import mba_deep_cleaning, change_1way_block_successor, create_block
from d810.hexrays_helpers import append_mop_if_not_in_list, extract_num_mop, CONTROL_FLOW_OPCODES,get_blk_index
from d810.optimizers.flow.flattening.generic import GenericDispatcherBlockInfo, GenericDispatcherUnflatteningRule
from d810.optimizers.flow.flattening.generic import GenericDispatcherInfo
from d810.optimizers.flow.flattening.unflattener import OllvmDispatcherCollector
from d810.optimizers.flow.flattening.utils import NotResolvableFatherException, get_all_possibles_values, \
    NotDuplicableFatherException
from d810.tracker import MopHistory, MopTracker
from d810.cfg_utils import change_1way_block_successor, change_2way_block_conditional_successor

from ida_hexrays import mblock_t, mop_t, optblock_t, minsn_visitor_t, mbl_array_t
import ida_hexrays as hr
import ida_kernwin as kw
import logging
from graphviz import Digraph
import os
from typing import List, Union, Tuple, Dict


FLATTENING_JUMP_OPCODES = [hr.m_jnz, hr.m_jz, hr.m_jae, hr.m_jb, hr.m_ja, hr.m_jbe, hr.m_jg, hr.m_jge, hr.m_jl,
                           hr.m_jle]

FAKE_LOOP_OPCODES = [hr.m_jz, hr.m_jnz]

opt_count = 0


os.environ['PATH'] = os.pathsep + r'C:\Program Files\Graphviz\bin'


def  showMicrocodeGraph(mba:mblock_t):
    dot = Digraph()

    for blk_idx in range(mba.qty):
        blk = mba.get_mblock(blk_idx)
        dot.node(str(blk_idx),)

        insn = blk.head
        index = 0
        # while insn:
        #     line = "{0}.{1}\t{2}    {3}".format(blk_idx, index, hex(insn.ea), insn.dstr())
        #     self.AddLine(line)
        #     self.insn_map[line_no] = insn
        #     index = index + 1
        #     line_no += 1
        #     if insn == blk.tail:
        #         break
        #
        #     insn = insn.next


class adjustOllvmDispatcherInfo(GenericDispatcherInfo):

    def explore(self, blk: mblock_t) -> bool:
        self.reset()
        if not self._is_candidate_for_dispatcher_entry_block(blk):
            return False
        self.entry_block = GenericDispatcherBlockInfo(blk)
        self.entry_block.parse()
        for used_mop in self.entry_block.use_list:
            append_mop_if_not_in_list(used_mop, self.entry_block.assume_def_list)
        self.dispatcher_internal_blocks.append(self.entry_block)
        num_mop, self.mop_compared = self._get_comparison_info(self.entry_block.blk)
        self.comparison_values.append(num_mop.nnn.value)
        self._explore_children(self.entry_block)
        dispatcher_blk_with_external_father = self._get_dispatcher_blocks_with_external_father()
        # TODO: I think this can be wrong because we are too permissive in detection of dispatcher blocks
        if len(dispatcher_blk_with_external_father) != 0:
            print("GenericDispatcherInfo can't blk_serial:",blk.serial)
            return False
        return True

    def _is_candidate_for_dispatcher_entry_block(self, blk: mblock_t) -> bool:
        # blk must be a condition branch with one numerical operand
        num_mop, mop_compared = self._get_comparison_info(blk)
        if (num_mop is None) or (mop_compared is None):
            return False
        # Its fathers are not conditional branch with this mop
        for father_serial in blk.predset:
            father_blk = self.mba.get_mblock(father_serial)
            father_num_mop, father_mop_compared = self._get_comparison_info(father_blk)
            if (father_num_mop is not None) and (father_mop_compared is not None):
                if mop_compared.equal_mops(father_mop_compared, hr.EQ_IGNSIZE):
                    return False
        return True

    def _get_comparison_info(self, blk: mblock_t) -> Tuple[mop_t, mop_t]:
        # We check if blk is a good candidate for dispatcher entry block: blk.tail must be a conditional branch
        if (blk.tail is None) or (blk.tail.opcode not in FLATTENING_JUMP_OPCODES):
            return None, None
        # One operand must be numerical
        num_mop, mop_compared = extract_num_mop(blk.tail)
        if num_mop is None or mop_compared is None:
            return None, None
        return num_mop, mop_compared

    def is_part_of_dispatcher(self, block_info: GenericDispatcherBlockInfo) -> bool:
        is_ok = block_info.does_only_need(block_info.father.assume_def_list)
        if not is_ok:
            return False
        if (block_info.blk.tail is not None) and (block_info.blk.tail.opcode not in FLATTENING_JUMP_OPCODES):
            return False
        return True

    def _explore_children(self, father_info: GenericDispatcherBlockInfo):
        for child_serial in father_info.blk.succset:
            if child_serial in [blk_info.blk.serial for blk_info in self.dispatcher_internal_blocks]:
                return
            if child_serial in [blk_info.blk.serial for blk_info in self.dispatcher_exit_blocks]:
                return
            child_blk = self.mba.get_mblock(child_serial)
            child_info = GenericDispatcherBlockInfo(child_blk, father_info)
            child_info.parse()
            if not self.is_part_of_dispatcher(child_info):
                self.dispatcher_exit_blocks.append(child_info)
            else:
                self.dispatcher_internal_blocks.append(child_info)
                if child_info.comparison_value is not None:
                    self.comparison_values.append(child_info.comparison_value)
                self._explore_children(child_info)

    def _get_external_fathers(self, block_info: GenericDispatcherBlockInfo) -> List[mblock_t]:
        internal_serials = [blk_info.blk.serial for blk_info in self.dispatcher_internal_blocks]
        external_fathers = []
        for blk_father in block_info.blk.predset:
            if blk_father not in internal_serials:
                external_fathers.append(blk_father)
        return external_fathers

    def _get_dispatcher_blocks_with_external_father(self) -> List[mblock_t]:
        dispatcher_blocks_with_external_father = []
        for blk_info in self.dispatcher_internal_blocks:
            if blk_info.blk.serial != self.entry_block.blk.serial:
                external_fathers = self._get_external_fathers(blk_info)
                if len(external_fathers) > 0:
                    dispatcher_blocks_with_external_father.append(blk_info)
        return dispatcher_blocks_with_external_father


## 收集 分发块信息，这个目前是写死的，后续可以通过主宰算法处理他
class adjustOllvmDispatcherCollector():
    DISPATCHER_CLASS = adjustOllvmDispatcherInfo
    DEFAULT_DISPATCHER_MIN_INTERNAL_BLOCK = 2
    DEFAULT_DISPATCHER_MIN_EXIT_BLOCK = 3
    DEFAULT_DISPATCHER_MIN_COMPARISON_VALUE = 2

    def __init__(self):
        super().__init__()
        self.dispatcher_list = []
        self.explored_blk_serials = []
        self.dispatcher_min_internal_block = self.DEFAULT_DISPATCHER_MIN_INTERNAL_BLOCK
        self.dispatcher_min_exit_block = self.DEFAULT_DISPATCHER_MIN_EXIT_BLOCK
        self.dispatcher_min_comparison_value = self.DEFAULT_DISPATCHER_MIN_COMPARISON_VALUE

    def get_dispatcher_list(self) -> List[GenericDispatcherInfo]:
        # self.collector(cur_blk)
        return self.dispatcher_list

    def set_dispatch_block(self, mba,serial_block) ->bool:
        blk = mba.get_mblock(serial_block)
        disp_info = self.DISPATCHER_CLASS(blk.mba)
        if disp_info.explore(blk):
            self.dispatcher_list.append(disp_info)
            return True
        else:
            return False

    # def collector(self, cur_blk):
    #     if cur_blk.serial == 2:
    #         global opt_count
    #         print("opt_count = ",opt_count)
    #         opt_count = opt_count + 1
    #         disp_info = self.DISPATCHER_CLASS(cur_blk.mba)
    #         if disp_info.explore(cur_blk):
    #             self.dispatcher_list.append(disp_info)


def insert_nop_blk(blk: mblock_t) -> mblock_t:
    # 这个函数的作用是在传入的块后面在复制一个代码块，然后指令全部变为nop
    # 另外需要修复一下控制流程，最后返回这个nop块
    # 为什么需要修复，因为copy_block 会将代码块插入制定的位置,但是他的使用文档中说，他不会修复前驱和后继
    mba = blk.mba
    nop_block = mba.copy_block(blk, blk.serial + 1)
    cur_ins = nop_block.head
    while cur_ins is not None:
        nop_block.make_nop(cur_ins)
        cur_ins = cur_ins.next

    nop_block.type = hr.BLT_1WAY

    # We might have clone a block with multiple or no successor, thus we need to clean all
    prev_successor_serials = [x for x in nop_block.succset]

    # Bookkeeping
    for prev_successor_serial in prev_successor_serials:
        nop_block.succset._del(prev_successor_serial)
        prev_succ = mba.get_mblock(prev_successor_serial)
        prev_succ.predset._del(nop_block.serial)
        if prev_succ.serial != mba.qty - 1:
            prev_succ.mark_lists_dirty()

    nop_block.succset.push_back(nop_block.serial + 1)
    nop_block.mark_lists_dirty()

    new_blk_successor = mba.get_mblock(nop_block.serial + 1)
    new_blk_successor.predset.push_back(nop_block.serial)
    if new_blk_successor.serial != mba.qty - 1:
        new_blk_successor.mark_lists_dirty()

    mba.mark_chains_dirty()
    try:
        mba.verify(True)
        return nop_block
    except RuntimeError as e:
        print("Error in insert_nop_blk: {0}".format(e))
        # print(nop_block, helper_logger.error)
        raise e



def duplicate_block(block_to_duplicate: mblock_t) -> Tuple[mblock_t, mblock_t]:
    # 这个函数的主要作用就是，复制一个代码块，并且取保这个代码块的后续执行流程和原代码块一致
    mba = block_to_duplicate.mba
    duplicated_blk = mba.copy_block(block_to_duplicate, mba.qty - 1)
    print("duplicate_block  Duplicated {0} -> {1}".format(block_to_duplicate.serial, duplicated_blk.serial))
    duplicated_blk_default = None
    if (block_to_duplicate.tail is not None) and hr.is_mcode_jcond(block_to_duplicate.tail.opcode):
        # 双分支，条件跳转判断
        # 这个代码的主要作用是让这个复制出来的，新插入的代码块的后续执行流程，跟被复制的代码块走一样的流程
        # 但是复制代码块会导致代码位置发生偏移，原代码顺序执行的位置代码位置，在新的代码块中是无法执行到得。
        # 所以先在制造一个代码块，就在上次复制代码块的后面，这样上次复制的代码块会顺序执行到第二次复制的位置。
        # 将第二次制造的代码块全部变为nop,然后 change_1way_block_successor,这个代码块最后一条指令goto的被复制代码顺序执行的位置。

        # block_to_duplicate.serial + 1 就是被复制的块的下一个块，双分支，这个块就是其中一个跳转的目标分支
        block_to_duplicate_default_successor = mba.get_mblock(block_to_duplicate.serial + 1)
        duplicated_blk_default = insert_nop_blk(duplicated_blk)
        change_1way_block_successor(duplicated_blk_default, block_to_duplicate.serial + 1)
        print("  {0} is conditional, so created a default child {1} for {2} which goto {3}"
                            .format(block_to_duplicate.serial, duplicated_blk_default.serial, duplicated_blk.serial,
                                    block_to_duplicate_default_successor.serial))
    elif duplicated_blk.nsucc() == 1:
        # 单分支，这里为什么要修改
        # 因为我们复制的这个模块的最后一条命令有可能不是goto命令，顺序执行也会直接执行到下一个代码块。
        # 如果是顺序执行，我们复制的这个代码块和原代码块代码不是位于同一个代码位置的，所以顺序执行不到目标的位置，需要我们把指令改成goto
        print("  Making {0} goto {1}".format(duplicated_blk.serial, block_to_duplicate.succset[0]))
        change_1way_block_successor(duplicated_blk, block_to_duplicate.succset[0])
    elif duplicated_blk.nsucc() == 0:
        print("  Duplicated block {0} has no successor => Nothing to do".format(duplicated_blk.serial))

    # 修复处理前驱
    # 在测试中发现ida的这个代码复制逻辑没有处理前驱，开始我并没有想到，后来调试中我发现，microcode的这个代码块cfg,结束块必须唯一最后一个位置，不能在结束块
    # 后面添加块，只能在前面添加，所以，复制了一个块，是在结束块的前面，这就导致，如果结束块的前驱是直接顺序执行到结束块的，在复制新块以后变成了执行到新的块，逻辑发生了改变
    duplicated_blk_pre = duplicated_blk.serial - 1
    duplicated_pre_blk = mba.get_mblock(duplicated_blk_pre)
    if duplicated_pre_blk.tail is not None:
        if duplicated_pre_blk.tail.opcode == hr.m_goto:
            print("{0} is_simple_goto_block ".format(duplicated_pre_blk.serial))
        else:
            print("change_1way_block_successor {0} -> {1}".format(duplicated_pre_blk.serial,duplicated_blk.serial+1),
            change_1way_block_successor(duplicated_pre_blk, duplicated_blk.serial+1))

    return duplicated_blk, duplicated_blk_default


def get_block_with_multiple_predecessors(var_histories: List[MopHistory]) -> Tuple[Union[None, mblock_t],
                                                                                   Union[None, Dict[int, List[MopHistory]]]]:

    for i, var_history in enumerate(var_histories):
        pred_blk = var_history.block_path[0]
        for block in var_history.block_path[1:]:
            tmp_dict = {pred_blk.serial: [var_history]}
            for j in range(i + 1, len(var_histories)):
                blk_index = get_blk_index(block, var_histories[j].block_path)
                if (blk_index - 1) >= 0:
                    other_pred = var_histories[j].block_path[blk_index - 1]
                    if other_pred.serial not in tmp_dict.keys():
                        tmp_dict[other_pred.serial] = []
                    tmp_dict[other_pred.serial].append(var_histories[j])
            if len(tmp_dict) > 1:
                return block, tmp_dict
            pred_blk = block
    return None, None


def try_to_duplicate_one_block(var_histories: List[MopHistory]) -> Tuple[int, int]:
    nb_duplication = 0
    nb_change = 0
    if (len(var_histories) == 0) or (len(var_histories[0].block_path) == 0):
        return nb_duplication, nb_change
    mba = var_histories[0].block_path[0].mba
    block_to_duplicate, pred_dict = get_block_with_multiple_predecessors(var_histories)
    if block_to_duplicate is None:
        return nb_duplication, nb_change
    print("Block to duplicate found: {0} with {1} successors"
                 .format(block_to_duplicate.serial, block_to_duplicate.nsucc()))
    i = 0  #pred_serial 是被复制块的前驱的块号，pred_history_group，是被复制块和他的这个前驱所在的分支
    for pred_serial, pred_history_group in pred_dict.items():
        # We do not duplicate first group
        if i >= 1:
            print("  Before {0}: {1}"
                         .format(pred_serial, [var_history.block_serial_path for var_history in pred_history_group]))
            pred_block = mba.get_mblock(pred_serial)
            #  返回两个块内，duplicated_blk_jmp 是复制的块，duplicated_blk_default 是 被复制块 多后继的时候产生的辅助块，复制duplicated_blk_jmp，可以跳转到 被复制块的顺序分支
            duplicated_blk_jmp, duplicated_blk_default = duplicate_block(block_to_duplicate)
            nb_duplication += 1 if duplicated_blk_jmp is not None else 0
            nb_duplication += 1 if duplicated_blk_default is not None else 0
            print("  Making {0} goto {1}".format(pred_block.serial, duplicated_blk_jmp.serial))
            if (pred_block.tail is None) or (not hr.is_mcode_jcond(pred_block.tail.opcode)):
                change_1way_block_successor(pred_block, duplicated_blk_jmp.serial)  # pred_block 的跳转目标地址变为 新复制出来的块
                nb_change += 1
            else:
                if block_to_duplicate.serial == pred_block.tail.d.b:             #  双分支的情况，判断需要修改那个跳转，找到跳转到被复制的那个块的位置，下面是双分支的单独修改
                    change_2way_block_conditional_successor(pred_block, duplicated_blk_jmp.serial)
                    nb_change += 1
                else:
                    print(" not sure this is suppose to happen")
                    change_1way_block_successor(pred_block.mba.get_mblock(pred_block.serial + 1),
                                                duplicated_blk_jmp.serial)
                    nb_change += 1

            block_to_duplicate_default_successor = mba.get_mblock(block_to_duplicate.serial + 1)
            print("  Now, we fix var histories...")        # 遍历分支，将已经进行的分支修改，在这里进行及时更新
            for var_history in pred_history_group:
                var_history.replace_block_in_path(block_to_duplicate, duplicated_blk_jmp)
                if block_to_duplicate.tail is not None and hr.is_mcode_jcond(block_to_duplicate.tail.opcode):
                    index_jump_block = get_blk_index(duplicated_blk_jmp, var_history.block_path)
                    if index_jump_block + 1 < len(var_history.block_path):
                        original_jump_block_successor = var_history.block_path[index_jump_block + 1]
                        if original_jump_block_successor.serial == block_to_duplicate_default_successor.serial:
                            var_history.insert_block_in_path(duplicated_blk_default, index_jump_block + 1)
        i += 1
        print("  After {0}: {1}"
                     .format(pred_serial, [var_history.block_serial_path for var_history in pred_history_group]))
    for i, var_history in enumerate(var_histories):
        print(" internal_pass_end.{0}: {1}".format(i, var_history.block_serial_path))
    return nb_duplication, nb_change

def duplicate_histories(var_histories: List[MopHistory], max_nb_pass: int = 10) -> Tuple[int, int]:
    cur_pass = 0
    total_nb_duplication = 0
    total_nb_change = 0
    print("Trying to fix new var_history...")
    for i, var_history in enumerate(var_histories):
        print(" start.{0}: {1}".format(i, var_history.block_serial_path))
    while cur_pass < max_nb_pass:
        print("Current path {0}".format(cur_pass))
        nb_duplication, nb_change = try_to_duplicate_one_block(var_histories)
        if nb_change == 0 and nb_duplication == 0:
            break
        total_nb_duplication += nb_duplication
        total_nb_change += nb_change
        cur_pass += 1
    for i, var_history in enumerate(var_histories):
        print(" end.{0}: {1}".format(i, var_history.block_serial_path))
    return total_nb_duplication, total_nb_change



class UnflattenerFakeJump(GenericDispatcherUnflatteningRule):
    DISPATCHER_COLLECTOR_CLASS = adjustOllvmDispatcherCollector
    DEFAULT_MAX_PASSES = 5
    DEFAULT_MAX_DUPLICATION_PASSES = 20

    def __init__(self):
        super().__init__()
        self.cur_blk = None
        self.dispatcher_collector = self.DISPATCHER_COLLECTOR_CLASS()
        self.dispatcher_list = []
        self.max_duplication_passes = self.DEFAULT_MAX_DUPLICATION_PASSES
        self.max_passes = self.DEFAULT_MAX_PASSES
        self.non_significant_changes = 0
        self.MOP_TRACKER_MAX_NB_BLOCK = 100
        self.MOP_TRACKER_MAX_NB_PATH = 100

    def func(self, blk: mblock_t):
        # import pydevd_pycharm
        # pydevd_pycharm.settrace('localhost', port=31235, stdoutToServer=True, stderrToServer=True)
        self.mba = blk.mba
        self.cur_blk = blk
        self.last_pass_nb_patch_done = 0
        if blk.mba.maturity != hr.MMAT_GLBOPT2:
            return 0
        if True == self.set_dispatch_block(19,blk):
            # now = datetime.now()
            # graphviz(self.mba,"/home/chic/unfla/"+now.strftime("%H-%M-%S-%f"))
            # if len(self.dispatcher_list) == 0:
            #     logging.info("No dispatcher found at maturity {0}".format(self.mba.maturity))
            #     return 0
            # print("dispatcher_list = ",len(self.dispatcher_list))
            self.last_pass_nb_patch_done = self.remove_flattening()
            print("Unflattening at maturity {0} pass {1}: {2} changes".format(self.cur_maturity, self.cur_maturity_pass, self.last_pass_nb_patch_done))
            nb_clean = mba_deep_cleaning(self.mba, False)  # 下面这几行可以删除，在这个项目好像没什么影响
            if self.last_pass_nb_patch_done + nb_clean + self.non_significant_changes > 0:
                self.mba.mark_chains_dirty()
                self.mba.optimize_local(0)
            self.mba.verify(True)
            return self.last_pass_nb_patch_done
        else:
            print("add dispatch failed")

        # fix fake jump loop
        if (blk.tail is None) or blk.tail.opcode not in FAKE_LOOP_OPCODES:
            return 0
        if blk.get_reginsn_qty() != 1:
            return 0
        if blk.tail.r.t != hr.mop_n:
            return 0
        op_compared = mop_t(blk.tail.l)
        blk_preset_list = [x for x in blk.predset]
        for pred_serial in blk_preset_list:
            cmp_variable_tracker = MopTracker([op_compared], max_nb_block=100, max_path=1000)
            cmp_variable_tracker.reset()
            pred_blk = blk.mba.get_mblock(pred_serial)
            pred_histories = cmp_variable_tracker.search_backward(pred_blk, pred_blk.tail)

            father_is_resolvable = all([father_history.is_resolved() for father_history in pred_histories])
            if not father_is_resolvable:
                return 0
            pred_values = get_all_possibles_values(pred_histories, [op_compared])
            pred_values = [x[0] for x in pred_values]
            if None in pred_values:
                print("Some path are not resolved, can't fix jump")
                return 0
            print("Pred {0} has {1} possible path ({2} different cst): {3}"
                               .format(pred_blk.serial, len(pred_values), len(set(pred_values)), pred_values))
            if self.fix_successor(blk, pred_blk, pred_values):
                self.last_pass_nb_patch_done += 1
        print("Unflattening at maturity {0} pass {1}: {2} changes".format(self.cur_maturity, self.cur_maturity_pass, self.last_pass_nb_patch_done))
        nb_clean = mba_deep_cleaning(self.mba, False)  # 下面这几行可以删除，在这个项目好像没什么影响
        if self.last_pass_nb_patch_done + nb_clean + self.non_significant_changes > 0:
            self.mba.mark_chains_dirty()
            self.mba.optimize_local(0)
        self.mba.verify(True)
        return self.last_pass_nb_patch_done


    def fix_successor(self, fake_loop_block: mblock_t, pred: mblock_t, pred_comparison_values: List[int]) -> bool:
        if len(pred_comparison_values) == 0:
            return False
        jmp_ins = fake_loop_block.tail
        compared_value = jmp_ins.r.nnn.value
        jmp_taken = False
        jmp_not_taken = False
        dst_serial = None
        if jmp_ins.opcode == hr.m_jz:
            jmp_taken = all([possible_value == compared_value for possible_value in pred_comparison_values])

            jmp_not_taken = all([possible_value != compared_value for possible_value in pred_comparison_values])
        elif jmp_ins.opcode == hr.m_jnz:
            jmp_taken = all([possible_value != compared_value for possible_value in pred_comparison_values])
            jmp_not_taken = all([possible_value == compared_value for possible_value in pred_comparison_values])
        # TODO: handles other jumps cases
        if jmp_taken:
            print("It seems that '{0}' is always taken when coming from {1}: {2}"
                               .format(format_minsn_t(jmp_ins), pred.serial, pred_comparison_values))
            dst_serial = jmp_ins.d.b
        if jmp_not_taken:
            print("It seems that '{0}' is never taken when coming from {1}: {2}"
                               .format(format_minsn_t(jmp_ins), pred.serial, pred_comparison_values))
            dst_serial = fake_loop_block.serial + 1
        if dst_serial is None:
            print("Jump seems legit '{0}' from {1}: {2}"
                                .format(format_minsn_t(jmp_ins), pred.serial, pred_comparison_values))
            return False

        return change_1way_block_successor(pred, dst_serial)



    def set_dispatch_block(self,block_serial,bllk) -> bool:
        if bllk.serial is not block_serial:
            return False
        self.dispatcher_list = []
        if True == self.dispatcher_collector.set_dispatch_block(self.mba,block_serial):

            self.dispatcher_list = [x for x in self.dispatcher_collector.get_dispatcher_list()]
            return True
        else:
            return False

    def graphviz(self ):
        dot = Digraph()
        dot.attr(splines='ortho')
        for blk_idx in range(self.mba.qty):
            blk = self.mba.get_mblock(blk_idx)
            if blk.head == None:
                continue
            lines = []

            lines.append("{0}:{1}".format(blk_idx, hex(blk.head.ea)))
            insn = blk.head
            while insn:
                lines.append(insn.dstr())
                if insn == blk.tail:
                    break
                insn = insn.next
            label = "\n".join(lines)
            dot.node(str(blk_idx), label=label, shape="rect", style="filled", fillcolor="lightblue")

        for blk_idx in range(self.mba.qty):
            blk = self.mba.get_mblock(blk_idx)
            succset = [x for x in blk.succset]
            for succ in succset:
                blk_succ = self.mba.get_mblock(succ)
                if blk_succ.head is None:
                    continue
                if blk.head is None:
                    continue
                dot.edge(str(blk_idx), str(succ))

        # dot.render("/home/chic/graph_with_contentgraph_with_content", format="png")
        output_path = "/home/chic/graph_with_content.dot"
        with open(output_path, "w") as f:
            f.write(dot.source)
        # dot.render("/home/chic/graph_with_content", format="png")
        print("dot已保存为 graph_with_content.png")


    def start(self):
        import pydevd_pycharm
        pydevd_pycharm.settrace('localhost', port=31235, stdoutToServer=True, stderrToServer=True)
        sel, sea, eea = kw.read_range_selection(None)
        pfn = ida_funcs.get_func(kw.get_screen_ea())
        if not sel and not pfn:
            return (False, "Position cursor within a function or select range")

        if not sel and pfn:
            sea = pfn.start_ea
            eea = pfn.end_ea
        print("fun addr:", hex(sea))
        addr_fmt = "%016x" if ida_ida.inf_is_64bit() else "%08x"
        fn_name = (ida_funcs.get_func_name(pfn.start_ea)
                   if pfn else "0x%s-0x%s" % (addr_fmt % sea, addr_fmt % eea))

        F = ida_bytes.get_flags(sea)
        if not ida_bytes.is_code(F):
            return (False, "The selected range must start with an instruction")
        text = "unfla"
        mmat = hr.MMAT_GLBOPT3
        if text is None and mmat is None:
            return (True, "Cancelled")

        if not sel and pfn:
            mbr = hr.mba_ranges_t(pfn)
        else:
            mbr = hr.mba_ranges_t()
            mbr.ranges.push_back(ida_range.range_t(sea, eea))

        hf = hr.hexrays_failure_t()
        ml = hr.mlist_t()
        #
        self.mba = hr.gen_microcode(mbr, hf, ml, hr.DECOMP_WARNINGS, mmat)

        self.retrieve_all_dispatchers()
        print("dispatcher_list = ", len(self.dispatcher_list))
        self.last_pass_nb_patch_done = self.remove_flattening()

    def retrieve_all_dispatchers(self):
        self.dispatcher_list = []
        self.dispatcher_list = [x for x in self.dispatcher_collector.get_dispatcher_list(self.cur_blk)]

    def remove_flattening(self) -> int:

        total_nb_change = 0
        for dispatcher_info in self.dispatcher_list:
            print("dispatcher_info:", hex(dispatcher_info.entry_block.blk.start))
            print("dispatcher_info predset len:", len(dispatcher_info.entry_block.blk.predset))
            dispatcher_father_list = [self.mba.get_mblock(x) for x in dispatcher_info.entry_block.blk.predset]
            for dispatcher_father in dispatcher_father_list:
                try:
                    total_nb_change += self.ensure_dispatcher_father_is_resolvable(dispatcher_father,dispatcher_info.entry_block)
                except NotDuplicableFatherException as e:
                    print(e)
                    pass
            if total_nb_change != 0:
                print("ensure_dispatcher_father_is_resolvable is changle,return")
                # self.graphviz()
                return total_nb_change
            dispatcher_father_list = [self.mba.get_mblock(x) for x in dispatcher_info.entry_block.blk.predset]
            print("start patch is len:",len(dispatcher_father_list))
            nb_flattened_branches = 0
            for dispatcher_father in dispatcher_father_list:
                try:
                    nb_flattened_branches += self.resolve_dispatcher_father(dispatcher_father, dispatcher_info)
                except NotResolvableFatherException as e:
                    print("NotResolvableFatherException")
                    print(e)
        return total_nb_change

    def ensure_dispatcher_father_is_resolvable(self, dispatcher_father: mblock_t,
                                               dispatcher_entry_block: GenericDispatcherBlockInfo) -> int:
        # if dispatcher_father.serial == 19:
        #     print("entry block 19")
            # showMicrocodeGraph(self.mba)
        father_histories = self.get_dispatcher_father_histories(dispatcher_father, dispatcher_entry_block)
        father_histories_cst = get_all_possibles_values(father_histories, dispatcher_entry_block.use_before_def_list,
                                                        verbose=False)
        father_is_resolvable = self.check_if_histories_are_resolved(father_histories)
        if not father_is_resolvable:
            raise NotDuplicableFatherException("Dispatcher {0} predecessor {1} is not duplicable: {2}"
                                               .format(dispatcher_entry_block.serial, dispatcher_father.serial,
                                                       father_histories_cst))
        for father_history_cst in father_histories_cst:
            if None in father_history_cst:
                raise NotDuplicableFatherException("Dispatcher {0} predecessor {1} has None value: {2}"
                                                   .format(dispatcher_entry_block.serial, dispatcher_father.serial,
                                                           father_histories_cst))

        print("Dispatcher {0} predecessor {1} is resolvable: {2}".format(dispatcher_entry_block.serial, dispatcher_father.serial, father_histories_cst))
        nb_duplication, nb_change = duplicate_histories(father_histories, max_nb_pass=self.max_duplication_passes)
        print("Dispatcher {0} predecessor {1} duplication: {2} blocks created, {3} changes made"
              .format(dispatcher_entry_block.serial, dispatcher_father.serial, nb_duplication, nb_change))
        return nb_duplication + nb_change

    def resolve_dispatcher_father(self, dispatcher_father: mblock_t, dispatcher_info) -> int:
        dispatcher_father_histories = self.get_dispatcher_father_histories(dispatcher_father,
                                                                           dispatcher_info.entry_block)
        father_is_resolvable = self.check_if_histories_are_resolved(dispatcher_father_histories)
        if not father_is_resolvable:
            raise NotResolvableFatherException("Can't fix block {0}".format(dispatcher_father.serial))
        mop_searched_values_list = get_all_possibles_values(dispatcher_father_histories,
                                                            dispatcher_info.entry_block.use_before_def_list,
                                                            verbose=False)

        ref_mop_searched_values = mop_searched_values_list[0]
        print("father_block:", dispatcher_father.serial)
        print("cvlist:", len(mop_searched_values_list))
        for tmp_mop_searched_values in mop_searched_values_list:
            if tmp_mop_searched_values != ref_mop_searched_values:
                raise NotResolvableFatherException("Dispatcher {0} predecessor {1} is not resolvable: {2}"
                                                   .format(dispatcher_info.entry_block.serial, dispatcher_father.serial,
                                                           mop_searched_values_list))

            # print("cv:", hex(tmp_mop_searched_values[0]))
        target_blk, disp_ins = dispatcher_info.emulate_dispatcher_with_father_history(dispatcher_father_histories[0])
        if target_blk is not None:
            print("Unflattening graph: Making {0} goto {1}"
                  .format(dispatcher_father.serial, target_blk.serial))
            ins_to_copy = [ins for ins in disp_ins if ((ins is not None) and (ins.opcode not in CONTROL_FLOW_OPCODES))]

            if len(ins_to_copy) > 0:
                print("Instruction copied: {0}: {1}"
                      .format(len(ins_to_copy),
                              ", ".join([format_minsn_t(ins_copied) for ins_copied in ins_to_copy])))
                dispatcher_side_effect_blk = create_block(self.mba.get_mblock(self.mba.qty - 2), ins_to_copy,
                                                          is_0_way=(target_blk.type == hr.BLT_0WAY))
                change_1way_block_successor(dispatcher_father, dispatcher_side_effect_blk.serial)
                change_1way_block_successor(dispatcher_side_effect_blk, target_blk.serial)
            else:
                print("else")
                change_1way_block_successor(dispatcher_father, target_blk.serial)
            return 2

    def get_dispatcher_father_histories(self, dispatcher_father: mblock_t,
                                        dispatcher_entry_block: GenericDispatcherBlockInfo) -> List[MopHistory]:
        father_tracker = MopTracker(dispatcher_entry_block.use_before_def_list,
                                    max_nb_block=self.MOP_TRACKER_MAX_NB_BLOCK, max_path=self.MOP_TRACKER_MAX_NB_PATH)
        father_tracker.reset()
        father_histories = father_tracker.search_backward(dispatcher_father, None)
        return father_histories

    def check_if_histories_are_resolved(self, mop_histories: List[MopHistory]) -> bool:
        return all([mop_history.is_resolved() for mop_history in mop_histories])


class blkOPt(hr.optblock_t):


    def func(self, blk):
        if blk.head is None:
            return 0
        # print(blk.mba.maturity, hex(blk.head.ea), blk.serial)
        if blk.mba.maturity != hr.MMAT_GLBOPT2:
            return 0
        optimizer = UnflattenerFakeJump()
        return optimizer.func(blk)


if __name__ == '__main__':  # 也可以直接在脚本里执行
    hr.clear_cached_cfuncs()

    # try:
    #     optimizer = UnflattenerFakeJump()
    #     optimizer.start()
    # except Exception as e:
    #     logging.exception(e)

    try:
        optimizer = blkOPt()
        optimizer.install()
        # optimizer.uninstall()
    except Exception as e:
        logging.exception(e)
