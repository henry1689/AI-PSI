"""测试包。

添加 ``__init__.py`` 是为了让 mypy 能以 ``tests.*`` 的形式匹配到测试模块——
没有它，每个测试文件都会被当作顶层模块，per-module override 会静默失效。
"""
