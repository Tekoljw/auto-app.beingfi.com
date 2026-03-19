/**
 * PwBrowse 入口：接收 PHP 传入的 JSON 参数，执行 viewReceipts / executePayment
 * 输出：最后一行为标准 JSON 结果，供 PHP 解析
 * 由 PHP shell_exec 调用时无 shell 环境，故在此加载 PwBrowse/.env
 */
require('dotenv').config({ path: require('path').join(__dirname, '.env') });

const path = require('path');
const { createBrowser } = require('./lib/browser');
const { loginKbiz, viewReceipts, executePayment, runBrowserSteps, runSingleStep, hasOtpInput, waitForOtp } = require('./lib/kbiz');

/** 未传入 website_url 时的默认登录页（兼容旧调用） */
const DEFAULT_LOGIN_URL = 'https://kbiz.kasikornbank.com/authen/';

async function main() {
  let input = {};
  const arg = process.argv[2];
  if (arg) {
    try {
      input = typeof arg === 'string' ? JSON.parse(arg) : arg;
    } catch (e) {
      outputResult({ success: false, error: 'Invalid JSON input: ' + e.message });
      process.exit(1);
    }
  }

  const logParams = { ...input };
  if (logParams.password !== undefined) logParams.password = '***';
  if (logParams.browserbase_api_key !== undefined) logParams.browserbase_api_key = '***';
  const fs = require('fs');
  const paramsLogPath = path.join(__dirname, 'node_last_params.json');
  try {
    fs.writeFileSync(paramsLogPath, JSON.stringify({ time: new Date().toISOString(), params: logParams }, null, 2), 'utf8');
  } catch (e) {}

  const action = input.action || 'viewReceipts';
  const username = input.username || '';
  const password = input.password || '';
  const cachePath = input.cache_path || path.join(__dirname, 'receipt_checked_cache.json');
  const otpApiUrl = input.otp_api_url || '';
  const captchaApiUrl = input.captcha_api_url || '';
  const otpWaitTimeoutSec = input.otp_wait_timeout_sec != null ? input.otp_wait_timeout_sec : 180;
  const otpPollIntervalSec = input.otp_poll_interval_sec != null ? input.otp_poll_interval_sec : 3;
  // 代理：由 PHP 传入。proxy_country 指定国家（如 TH）；可选 proxy_city、proxy_state（美国时）
  const proxyCountry = input.proxy_country != null ? String(input.proxy_country).trim() : '';
  const proxyCity = input.proxy_city != null ? String(input.proxy_city).trim() : '';
  const proxyState = input.proxy_state != null ? String(input.proxy_state).trim() : '';
  const useProxy = input.use_proxy === true || input.use_proxy === '1';

  const loginUrl = (input.website_url && String(input.website_url).trim()) ? String(input.website_url).trim() : DEFAULT_LOGIN_URL;
  const docDriven = input.doc_driven === true || input.doc_driven === '1';
  const getNextStepUrl = input.get_next_step_url || '';
  const reportResultUrl = input.report_result_url || '';
  const accountKey = input.account_key || '';
  const channelid = input.channelid != null ? Number(input.channelid) : 0;
  const channelType = input.channel_type != null ? Number(input.channel_type) : 2;

  // 统一生成“今天”日期字符串（本地时区），用于替换步骤中的 {{today}} 占位符
  function formatTodayLocal() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }
  const todayStr = formatTodayLocal();

  if (input.browserbase_api_key) process.env.BROWSERBASE_API_KEY = String(input.browserbase_api_key).trim();
  if (input.browserbase_project_id) process.env.BROWSERBASE_PROJECT_ID = String(input.browserbase_project_id).trim();

  if (!username || !password) {
    outputResult({ success: false, error: 'Missing username or password' });
    process.exit(1);
  }

  const proxyOption = proxyCountry
    ? { country: proxyCountry, city: proxyCity || undefined, state: proxyState || undefined }
    : useProxy
      ? true
      : undefined;

  let browser;
  try {
    browser = await createBrowser({ proxy: proxyOption });
    const context = browser.contexts()[0];
    let page = context.pages()[0];
    if (!page) page = await context.newPage();

    if (typeof page.setViewportSize === 'function') {
      await page.setViewportSize({ width: 1920, height: 1080 }).catch(() => {});
    }
    try {
      await context.setExtraHTTPHeaders({ 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8' });
    } catch (e) {}

    await page.goto(loginUrl, { waitUntil: 'networkidle', timeout: 30000 });

    if (docDriven && getNextStepUrl && reportResultUrl && channelid) {
      // 文档驱动：按通道文档单步取步→执行→上报（failure_type：success/step_failure/account_failure），直到 viewReceipts/executePayment
      let lastResult = null;
      let lastCompletedStepId = '';
      const maxRounds = 50;

      function failureType(result) {
        if (result && result.success) return 'success';
        const err = (result && result.error) ? String(result.error) : '';
        const msg = err.toLowerCase();
        // 元素/页面结构相关：视为步骤错误
        if (/未找到输入框|未找到可点击元素|未找到|selector|element|No receipts menu|Parse receipts error|导航失败|页面不符合预期/i.test(err)) {
          return 'step_failure';
        }
        // 手机验证码 / 密码 / 账户状态等账号侧错误
        if (/密码错误|用户名或密码错误|账号.*错误|账户.*冻结|余额不足|insufficient|sms|手机验证码|短信验证码|otp|one[-\s]?time code/i.test(err)) {
          return 'account_failure';
        }
        // 图形/滑块验证码识别失败：更偏向脚本能力问题
        if (/图片验证码识别失败|captcha solve failed|captcha recognition failed/i.test(err)) {
          return 'step_failure';
        }
        // 超时类：短信验证码/等待验证码超时归为账号侧，其余保守按步骤错误处理
        if (/等待验证码超时|验证码超时|otp timeout/i.test(err)) {
          return 'account_failure';
        }
        if (/timeout|超时/.test(err)) {
          return 'step_failure';
        }
        // 兜底：默认按步骤错误处理，避免错误脚本长期保留
        return 'step_failure';
      }

      for (let round = 0; round < maxRounds; round++) {
        // 每一轮开始前，若有新 tab/page 打开，则始终使用最新的 page
        try {
          const pages = context.pages();
          if (pages && pages.length > 0) {
            page = pages[pages.length - 1];
          }
        } catch (e) {}
        const pageUrl = page.url();
        let screenshotBase64 = '';
        try {
          screenshotBase64 = await page.screenshot({ encoding: 'base64', type: 'jpeg', quality: 60 }).catch(() => '');
        } catch (e) {}
        const body = JSON.stringify({
          channelid,
          account_key: accountKey,
          pageContext: { url: pageUrl, screenshot_base64: screenshotBase64 || undefined },
          last_completed_step_id: lastCompletedStepId || undefined,
        });
        let res;
        try {
          res = await fetch(getNextStepUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
        } catch (e) {
          outputResult({ success: false, error: 'getNextStep 请求失败: ' + (e.message || e) });
          await browser.close();
          process.exit(1);
        }
        const data = await res.json().catch(() => ({}));
        const step = data.step;
        const done = data.done === true;
        if (done || !step) {
          if (lastResult) outputResult(lastResult);
          else outputResult({ success: false, error: data.error || '无下一步且无已执行结果' });
          await browser.close();
          process.exit(done && lastResult ? 0 : 1);
        }

        const stepType = (step.type || '').toLowerCase();
        let result = {};

        async function waitForPageSettle(page, timeoutMs) {
          // 某些站点 click 后会异步渲染/跳转，短暂等待页面更“稳”再做 selector 判定
          const t = timeoutMs != null ? timeoutMs : 5000;
          try {
            await page.waitForLoadState('domcontentloaded', { timeout: t }).catch(() => {});
            await page.waitForLoadState('networkidle', { timeout: t }).catch(() => {});
          } catch (e) {}
        }

        async function waitForOtpAppear(page, timeoutMs) {
          const deadline = Date.now() + timeoutMs;
          while (Date.now() < deadline) {
            if (await hasOtpInput(page)) return true;
            await page.waitForTimeout(300);
          }
          return false;
        }

        if (stepType === 'fill' || stepType === 'click') {
          // 文档驱动步骤支持占位符：{{username}} / {{password}} / {{today}}
          result = await runSingleStep(page, step, { username, password, today: todayStr });
          if (result.success) {
            lastCompletedStepId = step.id || '';

            // 每步成功后，重新获取当前最新的 page（处理点击后新开 tab 的场景），再检查是否进入短信验证码页
            try {
              const pagesAfter = context.pages();
              if (pagesAfter && pagesAfter.length > 0) {
                page = pagesAfter[pagesAfter.length - 1];
              }
            } catch (e) {}

            // 仅在 click 步骤后等待 OTP：避免每次 fill 都白等导致变慢
            if (stepType === 'click' && otpApiUrl) {
              await waitForPageSettle(page, 6000);
              const selHint = ((step && step.selector) ? String(step.selector) : '').toLowerCase();
              const isLikelyOtpTrigger = /登录|下一步|确认|subbut|submit|authen|login/.test(selHint);
              const otpWaitMs = isLikelyOtpTrigger ? 20000 : 8000;
              if (await waitForOtpAppear(page, otpWaitMs)) {
              const otpResult = await waitForOtp(page, username, otpApiUrl, otpWaitTimeoutSec, otpPollIntervalSec);
              if (!otpResult.ok) {
                result = { success: false, error: otpResult.otp_result === 'wrong' ? '验证码错误' : '等待验证码超时', otp_result: otpResult.otp_result };
              } else {
                await page.waitForTimeout(2000);
              }
              }
            }
          }
        } else if (stepType === 'viewreceipts') {
          result = await viewReceipts(page, cachePath, {
            receipt_parse_url: input.receipt_parse_url || '',
            channelid,
            account_key: accountKey,
            receive_params: input.receive_params || null,
          });
          lastResult = result;
        } else if (stepType === 'executepayment') {
          const paymentLogPath = path.join(__dirname, 'node_payment.log');
          const payLog = (msg) => {
            try {
              require('fs').appendFileSync(paymentLogPath, new Date().toISOString() + ' ' + msg + '\n', 'utf8');
            } catch (e) {}
          };
          const orders = input.orders || [];
          payLog('[doc_executepayment] start orders_count=' + orders.length);
          result = await executePayment(page, {
            amount: step.amount || input.amount || '',
            payee: step.payee || input.payee || '',
            memo: step.memo || input.memo || '',
          }, cachePath, {
            otp_api_url: otpApiUrl,
            captcha_api_url: captchaApiUrl,
            otp_wait_timeout_sec: otpWaitTimeoutSec,
            otp_poll_interval_sec: otpPollIntervalSec,
            username,
            captcha_box_selector: input.captcha_box_selector,
            payment_params: input.payment_params || null,
            pay_params: input.pay_params || null,
            orders,
            logFn: payLog,
          });
          payLog('[doc_executepayment] end success=' + (result && result.success));
          lastResult = result;
        } else {
          result = { success: false, error: 'Unknown step type: ' + stepType };
        }

        const failure_type = failureType(result);
        const logPath = path.join(__dirname, 'node_report.log');
        const errMsg = (result && result.error) ? String(result.error).slice(0, 300) : '';
        try {
          require('fs').appendFileSync(logPath, `${new Date().toISOString()} result_error=${errMsg}\n`);
          require('fs').appendFileSync(logPath, `${new Date().toISOString()} before_report channelid=${channelid} step_id=${(step && step.id) || ''} failure_type=${failure_type}\n`);
          const reportRes = await fetch(reportResultUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channelid, step, result, failure_type }),
          });
          const reportText = await reportRes.text();
          require('fs').appendFileSync(logPath, `${new Date().toISOString()} after_report status=${reportRes.status} body=${reportText.slice(0, 200)}\n`);
        } catch (e) {
          require('fs').appendFileSync(logPath, `${new Date().toISOString()} report_error ${e.message || e}\n`);
        }

        if (stepType === 'viewreceipts' || stepType === 'executepayment') {
          outputResult(lastResult);
          await browser.close();
          process.exit(0);
        }
        if (result.success) {
          lastCompletedStepId = step.id || '';
        }
      }
      outputResult(lastResult || { success: false, error: '文档驱动循环达到上限' });
      await browser.close();
      process.exit(0);
    }

    const loggedIn = await loginKbiz(page, username, password, {
      otp_api_url: otpApiUrl,
      otp_wait_timeout_sec: otpWaitTimeoutSec,
      otp_poll_interval_sec: otpPollIntervalSec,
    });
    if (typeof loggedIn === 'object' && loggedIn && loggedIn.success === false) {
      outputResult({ success: false, error: loggedIn.error || 'OTP failed', otp_result: loggedIn.otp_result });
      await browser.close();
      process.exit(1);
    }
    if (!loggedIn) {
      outputResult({ success: false, error: 'Login failed' });
      await browser.close();
      process.exit(1);
    }

    // 登录流程可能打开新 tab/page，后续操作始终使用最新的 page
    try {
      const pages = context.pages();
      if (pages && pages.length > 0) {
        page = pages[pages.length - 1];
      }
    } catch (e) {}

    if (action === 'viewReceipts') {
      const result = await viewReceipts(page, cachePath, {
        receipt_parse_url: input.receipt_parse_url || '',
        channelid,
        account_key: accountKey,
        receive_params: input.receive_params || null,
      });
      outputResult(result);
    } else if (action === 'executePayment') {
      const paymentLogPath = path.join(__dirname, 'node_payment.log');
      const payLog = (msg) => {
        try {
          require('fs').appendFileSync(paymentLogPath, new Date().toISOString() + ' ' + msg + '\n', 'utf8');
        } catch (e) {}
      };
      const orders = input.orders || [];
      payLog('[executePayment] start username=' + username + ' orders_count=' + orders.length + ' has_payment_params=' + !!(input.payment_params || input.pay_params));
      const result = await executePayment(page, {
        amount: input.amount || '',
        payee: input.payee || '',
        memo: input.memo || '',
      }, cachePath, {
        otp_api_url: otpApiUrl,
        captcha_api_url: captchaApiUrl,
        otp_wait_timeout_sec: otpWaitTimeoutSec,
        otp_poll_interval_sec: otpPollIntervalSec,
        username,
        captcha_box_selector: input.captcha_box_selector,
        payment_params: input.payment_params || null,
        pay_params: input.pay_params || null,
        orders,
        logFn: payLog,
      });
      payLog('[executePayment] end success=' + (result && result.success) + ' order_results_count=' + (result && result.order_results ? result.order_results.length : 0));
      outputResult(result);
    } else {
      outputResult({ success: false, error: 'Unknown action: ' + action });
    }

    await browser.close();
  } catch (err) {
    outputResult({ success: false, error: err.message || String(err) });
    if (browser) await browser.close().catch(() => {});
    process.exit(1);
  }
}

function outputResult(obj) {
  console.log(JSON.stringify(obj));
}

main();
