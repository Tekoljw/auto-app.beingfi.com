# PwBrowse

Playwright + Browserbase，用于 K BIZ 登录、查看收款、执行付款等浏览器自动化。

## 环境

- Node.js **16.14.2+** 或 18+（当前锁定 playwright-core@1.35 以兼容 Node 16）
- 在 [Browserbase](https://www.browserbase.com/) 创建项目，获取 API Key 与 Project ID

## 安装

```bash
cd PwBrowse
npm install
```

复制环境变量并填写：

```bash
cp .env.example .env
# 编辑 .env：BROWSERBASE_API_KEY、BROWSERBASE_PROJECT_ID
```

## 使用方式

由 PHP 调用（推荐）。代理由 PHP 配置传入，见 [Browserbase Proxies](https://docs.browserbase.com/features/proxies)：

```bash
node run.js '{"action":"viewReceipts","username":"Bravocc","password":"Abc@12345","cache_path":"/path/to/receipt_checked_cache.json"}'
# 指定国家代理：proxy_country（必填）, proxy_city、proxy_state（可选）
node run.js '{"action":"viewReceipts","username":"Bravocc","password":"Abc@12345","proxy_country":"TH","proxy_city":"BANGKOK"}'
node run.js '{"action":"executePayment","username":"Bravocc","password":"Abc@12345","amount":"100","payee":"xxx","memo":"备注","proxy_country":"TH"}'
```

本地调试：

```bash
npm run view-receipts
# 或
node run.js '{"action":"viewReceipts","username":"你的账号","password":"你的密码"}'
```

## 输出

脚本最后一行输出为 JSON，供 PHP 解析，例如：

- 查看收款：`{"success":true,"receipts":[...],"checked_receipts":[...],"summary":"...","new_count":n}`
- 执行付款：`{"success":true,"payment_id":"pay_xxx","amount":"...","payee":"...","summary":"..."}`

## 选择器说明

`lib/kbiz.js` 中的 `SELECTORS` 为 K BIZ 页面的占位选择器，实际运行前请按真实页面（class/id/文案）修改，否则可能无法正确找到登录框、收款列表、转账表单等元素。
