"""pytest 全局配置。

- ``test_model_routing.py`` 为手动集成脚本（函数签名接收 MasterAgent 实例，
  非 pytest fixture 语义），默认套件不收集；手动执行::

      python tests/test_model_routing.py
"""

collect_ignore = ["test_model_routing.py"]
