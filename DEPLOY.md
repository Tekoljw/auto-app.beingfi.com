# 服务器部署说明（自动网银交换）

本说明帮助你在 Linux 服务器上把「PHP + Node(PwBrowse)」跑起来，并配置定时或手动执行。

---

## 一、环境要求

| 组件 | 版本要求 | 说明 |
|------|----------|------|
| PHP | 5.4+（建议 7.x） | 需开启 CLI，且能执行 `shell_exec('node ...')` |
| Node.js | **16.14.2+** 或 **18+** | 当前 PwBrowse 使用 playwright-core@1.35 以兼容 Node 16；若系统 glibc 较旧无法装 Node 18，用 Node 16 即可 |
| MySQL | - | 存渠道/账号（tw_paytype_config、tw_payparams_list 等） |
| 网络 | 出网 | Node 需连 Browserbase、目标网站；PHP 需被 Node 回调（getOtp、recognizeCaptcha、**getNextStep**、**reportStepResult**） |

---

## 二、上传代码

1. 把整个项目放到服务器目录，例如：
   ```text
   /var/www/auto-app.beingfi.com/   （或你的站点根目录）
   ├── index.php
   ├── Application/
   │   └── Cli/
   │       └── Controller/
   │           └── AutoWebExchangeController.class.php
   ├── PwBrowse/
   │   ├── run.js
   │   ├── lib/
   │   ├── package.json
   │   └── .env
   ├── Runtime/
   └── ...
   ```

2. 确保 **Runtime** 与 **Public/WebAutoScriptDoc** 可写（Runtime：缓存等；WebAutoScriptDoc：每通道一份操作文档，步骤带权重）：
   ```bash
   chmod 755 /var/www/auto-app.beingfi.com/Runtime
   mkdir -p /var/www/auto-app.beingfi.com/Public/WebAutoScriptDoc
   chmod 755 /var/www/auto-app.beingfi.com/Public/WebAutoScriptDoc
   # 若 PHP 以 www-data 运行：
   chown -R www-data:www-data /var/www/auto-app.beingfi.com/Runtime /var/www/auto-app.beingfi.com/Public/WebAutoScriptDoc
   ```

---

## 三、PHP 配置

1. **数据库**  
   在 ThinkPHP 配置里配好数据库连接、表前缀（如 `tw_`），保证能访问：
   - `tw_paytype_config`
   - `tw_payparams_list`
   - `tw_exchange_auto_payment_code`（验证码表）

2. **本模块用到的配置项**（写入你的配置数组或配置文件）：
   ```php
   // Node 回调 PHP 的根 URL（必须能从「运行 Node 的同一台机」访问到）
   'AUTO_WEB_EXCHANGE_BASE_URL' => 'https://你的域名或IP',

   // 可选：收款金额列
   'RECEIPT_AMOUNT_COLUMN_INDEX' => 3,
   // 或 'RECEIPT_AMOUNT_SELECTOR' => '.amount',

   // 可选：OTP 超时与轮询
   'AUTO_WEB_EXCHANGE_OTP_TIMEOUT' => 180,
   'AUTO_WEB_EXCHANGE_OTP_POLL_INTERVAL' => 3,

   // 是否使用代理（唯一开关）：false 或不设 = 不使用代理（免费版）；true = 使用代理，代理参数从数据库渠道表获取
   'AUTO_WEB_EXCHANGE_USE_PROXY' => false,
   // 仅当 USE_PROXY 为 true 且渠道未配置时代理国家/城市时，用下面配置补全
   'AUTO_WEB_EXCHANGE_PROXY_COUNTRY' => 'TH',
   'AUTO_WEB_EXCHANGE_PROXY_CITY'    => 'BANGKOK',

   // Browserbase（由 PHP 传入 Node，可不配 .env）
   'BROWSERBASE_API_KEY'    => '你的 API Key',
   'BROWSERBASE_PROJECT_ID' => '你的 Project ID',
   ```

   **说明**：`AUTO_WEB_EXCHANGE_BASE_URL` 是 Node 请求 getOtp、recognizeCaptcha、getNextStep、reportStepResult 的根地址，必须 **HTTP 可访问**（文档驱动：打开页面后读文档→无下一步则生成登录步骤或调 AI→执行并写回，Node 会循环调 getNextStep/reportStepResult）。

3. **关闭禁用函数（若用 shell_exec 调 Node）**  
   在 `php.ini` 中确保未禁用 `shell_exec`：
   ```ini
   ; 不要在这里列出 shell_exec
   disable_functions = ...
   ```

---

## 四、Node（PwBrowse）配置

1. 进入 PwBrowse 目录并安装依赖：
   ```bash
   cd /var/www/auto-app.beingfi.com/PwBrowse
   npm install
   ```

2. 配置 Browserbase 环境变量（与 .env.example 一致）：
   ```bash
   cp .env.example .env
   # 编辑 .env，填入你的 Browserbase 信息
   nano .env
   ```
   ```env
   BROWSERBASE_API_KEY=你的API_KEY
   BROWSERBASE_PROJECT_ID=你的PROJECT_ID
   ```

3. 用 Node 直接跑时，需让 Node 进程能读到 .env。若通过 PHP 的 `shell_exec('node run.js ...')` 调用，通常不会自动加载 .env，有两种做法：
   - **推荐**：在 PHP 里执行前导出环境变量（见下节「运行方式」），或
   - 在服务器上对运行 PHP 的用户配置好环境变量（例如 systemd 的 `Environment=` 或 `/etc/environment`）。

---

## 五、运行方式

### 1. 命令行手动执行（推荐先这样验证）

在**项目根目录**（与 index.php、Application 同级）执行：

```bash
cd /www/wwwroot/test-otc-api.beingfi.com
php index.php Cli AutoWebExchange index
```

或路径写法（视 ThinkPHP 版本）：

```bash
php index.php Cli/AutoWebExchange/index
```

执行成功会在终端输出一行 JSON（如 `{"code":0,"msg":"ok","data":...}`）。

### 2. 定时任务（cron）

希望按固定频率跑（例如每 5 分钟一次）：

```bash
crontab -e
```

添加（按需改路径和频率）：

```cron
# 每 5 分钟执行一次
*/5 * * * * cd /www/wwwroot/test-otc-api.beingfi.com && /usr/bin/php index.php Cli AutoWebExchange index >> /var/log/auto_web_exchange.log 2>&1
```

若 Node 需要读 .env，可先导出再执行：

```cron
*/5 * * * * cd /www/wwwroot/test-otc-api.beingfi.com/PwBrowse && export $(grep -v '^#' .env | xargs) && cd .. && /usr/bin/php index.php Cli AutoWebExchange index >> /var/log/auto_web_exchange.log 2>&1
```

（或把 `BROWSERBASE_API_KEY`、`BROWSERBASE_PROJECT_ID` 写到系统环境变量，则不必在 cron 里 export .env。）

### 3. 确保 Node 能被找到

PHP 里用的是 `node` 命令，需在运行 PHP 的环境里能访问到：

```bash
which node   # 例如 /usr/bin/node 或 /home/xxx/.nvm/versions/node/v18.x/bin/node
```

若 PHP 是 apache/nginx 通过 php-fpm 跑，cron 用到的 shell 和 fpm 环境可能不同：**建议用 cron 执行 CLI**（如上），这样 Node 由 cron 的 PATH 解析即可。

---

## 六、校验清单

- [ ] 数据库可连，`tw_paytype_config`、`tw_payparams_list` 有 is_web=1 的渠道和对应账号  
- [ ] `Runtime/` 可写  
- [ ] `AUTO_WEB_EXCHANGE_BASE_URL` 在服务器本机可访问（curl 或 Node 里 fetch 能通）  
- [ ] PwBrowse 下 `npm install` 成功，且 `BROWSERBASE_API_KEY`、`BROWSERBASE_PROJECT_ID` 已配置  
- [ ] 命令行执行 `php index.php Cli AutoWebExchange index` 有正常 JSON 输出  
- [ ] 需要定时跑时，cron 已添加且日志路径可写（如 `/var/log/auto_web_exchange.log`）

---

## 七、常见问题

1. **命令行执行后没有任何输出（日志有 app_init 等，但终端无 JSON）**  
   - 先打开错误输出：  
     `php -d display_errors=1 -d log_errors=1 index.php Cli AutoWebExchange index`  
   - 试路径写法：`php index.php Cli/AutoWebExchange/index`。  
   - 确认项目已配置 CLI 路由（如 BIND_MODULE 或 index.php 中为 CLI 设置 REQUEST_URI / $_GET['m']['c']['a']）。

2. **Node 报错找不到或执行失败**  
   用 cron 时用绝对路径：`/usr/bin/node`，并保证 `run.js` 用绝对路径传入（当前代码里 PHP 已用 `$this->nodeScriptPath` 绝对路径）。

3. **Node 报 Missing BROWSERBASE_API_KEY**  
   Node 进程未读到 .env：在 cron 里 `export` 上述变量，或在 systemd/服务器环境里配置好再执行 PHP。

4. **getOtp / recognizeCaptcha 请求失败**  
   检查 `AUTO_WEB_EXCHANGE_BASE_URL` 是否可从本机访问，以及防火墙/安全组是否放行。

5. **没有执行任何步骤**  
   看 `Runtime/auto_web_exchange_ops.json` 里是否有 `pending_steps`，以及数据库渠道/账号是否满足 is_web=1 且 website_url 非空。

按以上步骤即可在服务器上跑起来；若要改执行频率或日志路径，只需调整 cron 行即可。
