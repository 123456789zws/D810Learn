# D810Learn


## ida函数
+ 用于清楚反编译的结果（关闭反编译界面，执行函数，会重新开始反编译，用于测试反编译优化非常有用）

import ida_hexrays
ida_hexrays.clear_cached_cfuncs()


## ida 插件
### d810

+ 地址：https://gitlab.com/eshard/d810
+ 地址：https://gitlab.com/eshard/d810_samples
+ https://github.com/gaasedelen/lucid


## id脚本

+ vds5.py ida官方脚本，可以用来查看类似ast

## 总结教训1

反流程平坦化还原，必须在 多分支代码块ssa之后，否则可能会导致平坦化将分发块还原了，导致代码块ssa的时候无法找到 平坦化的的常量指令