"""集中存放测试用的**假密钥**。

为什么这些字符串"长得像真密钥"
--------------------------------
打码 / 脱敏 / 形态兜底这类功能，测试输入**必须**是真实形态
（`sk-` 前缀 + 足够长度），否则测的是"随便一个字符串会不会被遮"，
等于没测。所以这里保留了能被各家密钥正则命中的串。

**这里没有任何一个是真密钥**，请勿：

  * 拿去请求任何真实服务；
  * 复制进配置、示例、文档；
  * 凭它们去推断真实密钥的构造方式。

对外公开时看到本文件属正常：这是测试夹具的隔离存放点。
若某个密钥扫描器对本文件报警，那是**预期行为**。

改动须知
--------
新增假值时，**必须**同步登记到 `.githooks/pre-commit` 的 `ALLOW_SAMPLES`
（整串精确匹配），否则提交会被那道闸拦下。
"""

# 通用假密钥：用于 mask / redact 的基本行为
FAKE_KEY = "sk-abcdef0123456789abcdef0123456789"

# 没有登记到 register_secret() 的密钥：验证「形态兜底」也能遮住
FAKE_UNREGISTERED_KEY = "sk-unregistered-key-123456"

# Authorization 头里的 Bearer token（不带 sk- 前缀，走另一条正则分支）
FAKE_BEARER = "Bearer abcdefghijklmnop1234"

# 用户自己配在环境变量里的密钥：验证 clear() 不会把它一起抹掉
FAKE_ENV_KEY = "sk-original-from-system-env"

# 对话文本里出现的密钥：验证记忆抽取时能识别并打码
FAKE_KEY_IN_DIALOG = "sk-ZZZZZZZZZZZZZZZZZZZZ"
FAKE_KEY_IN_DIALOG_ALT = "sk-abcdefghijklmnopqrstuvwxyz123456"

# GitHub token 形态（仅测试 token 识别逻辑，从不用于任何真实仓库）
FAKE_GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuv"
