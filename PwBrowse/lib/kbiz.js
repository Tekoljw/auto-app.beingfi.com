/**
 * K BIZ 登录、查看收款记录、执行付款
 * 支持：登录/转账时等待短信验证码（轮询 PHP getOtp）；转账图片验证码 OCR + PHP AI 兜底
 */
const fs = require('fs');
const path = require('path');
const AI_HARD_ERROR_TOKEN = '__AI_HARD_ERROR__';

/** 登录页：用户名、密码、登录按钮（兼容 K BIZ 泰文、建行等中文网银） */
const SELECTORS = {
  login: {
    username: 'input[name="username"], input[name="USERID"], input[id*="user"], input[id*="USER"], input[placeholder*="รหัส"], input[placeholder*="用户名"], input[placeholder*="账号"], input[placeholder*="手机"]',
    password: 'input[name="password"], input[name="LOGINPWD"], input[type="password"]',
    submit: 'button[type="submit"], input[type="submit"], input[value="登录"], button:has-text("登录"), a:has-text("登录"), button:has-text("安全登录"), a:has-text("เข้าสู่ระบบ"), button:has-text("เข้าสู่ระบบ")',
  },
  /** 短信验证码页（登录或转账后可能出现；新通道在此增加选择器即可） */
  otp: {
    input: 'input[name="otp"], input[name="verifyCode"], input[name="smsCode"], input[name="TRANSMSCODE"], input[placeholder*="验证码"], input[placeholder*="附加码"], input[placeholder*="短信动态口令"]',
    submit: 'button[type="submit"]:has-text("确认"), button:has-text("确认"), input[value="确认"], input[value="确 认"]',
    errorMsg: '.error, .err-msg, [class*="error"], [class*="err"]',
  },
  /** 转账页图片验证码（附加码；新通道在此增加选择器即可） */
  captcha: {
    box: 'img[src*="captcha"], img[src*="verify"], .captcha img, [class*="captcha"] img, table td img',
    input: 'input[name="additionalCode"], input[name="captcha"], input[name="TRANEXTRACODE"], input[id="TRANEXTRACODE"], input[placeholder*="附加码"]',
    refreshLink: 'a:has-text("换一张"), a:has-text("看不清楚"), [href*="refresh"]',
  },
  receipts: {
    menuLink: 'a[href*="receipt"], a:has-text("收款"), a:has-text("รับเงิน"), a[href*="transaction"]',
    table: 'table tbody tr, [data-role="receipt-list"] .item, .receipt-row',
    timeCell: 'td:nth-child(1), td:nth-child(2), .time, .date',
    amountCell: 'td:nth-child(3), .amount, [class*="amount"]',
    rowId: 'td:first-child, [data-id]',
  },
};

/** 拟人化：随机延迟 min～max 毫秒 */
function randomDelay(minMs, maxMs) {
  // 下调默认停顿，减少整体耗时（仍保留少量随机性）
  const min = minMs == null ? 150 : minMs;
  const max = maxMs == null ? 450 : maxMs;
  const ms = Math.floor(Math.random() * (max - min + 1)) + min;
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** 模拟人工输入：先 focus，再逐字 type 带随机间隔（避免陷阱 + 拟人） */
async function typeLikeHuman(page, selector, text, options = {}) {
  const el = await findVisible(page, selector);
  if (!el) return false;
  await el.click(options.forceClick ? { force: true } : {});
  await randomDelay(100, 300);
  // 下调逐字输入延迟，减少体感“太慢”
  const delayMin = options.delayMin != null ? options.delayMin : 30;
  const delayMax = options.delayMax != null ? options.delayMax : 90;
  const str = String(text);
  for (let i = 0; i < str.length; i++) {
    await page.keyboard.type(str[i], { delay: Math.floor(Math.random() * (delayMax - delayMin + 1)) + delayMin });
  }
  return true;
}

/** 在多个选择器中找到第一个可见元素；先主页面再各 iframe（建行等登录在 iframe 内） */
async function findVisible(page, selector) {
  const selectors = Array.isArray(selector) ? selector : [selector];
  for (const sel of selectors) {
    try {
      let el = await page.$(sel);
      if (el && (await el.isVisible())) return el;
      const frames = page.frames();
      for (const frame of frames) {
        if (frame === page.mainFrame()) continue;
        el = await frame.$(sel);
        if (el && (await el.isVisible())) return el;
      }
    } catch (e) {
      // continue
    }
  }
  return null;
}

/** 仅当元素可见时点击，并带点击前随机停顿 */
async function clickVisible(page, selector, options = {}) {
  const el = await findVisible(page, selector);
  if (!el) return false;
  // 下调点击前停顿
  await randomDelay(options.beforeClickMin != null ? options.beforeClickMin : 200, options.beforeClickMax != null ? options.beforeClickMax : 600);
  await el.click();
  return true;
}

/**
 * 轮询 PHP getOtp 接口获取验证码，填入并提交；检测错误或成功
 * @param {string} [inputSelector] 短信验证码输入框选择器，未传则用 SELECTORS.otp.input
 * @param {string} [submitSelector] 提交按钮选择器，未传则用 SELECTORS.otp.submit
 * @param {{ submitAfterFill?: boolean }} [options] submitAfterFill 为 false 时只填不点提交（由调用方填完图形验证码后统一点一次）
 * @returns {Promise<{ ok: boolean, otp_result: 'ok'|'wrong'|'timeout' }>}
 */
async function waitForOtp(page, account, otpApiUrl, timeoutSec, pollIntervalSec, inputSelector, submitSelector, options = {}) {
  if (!otpApiUrl || !account) {
    return { ok: false, otp_result: 'timeout' };
  }
  const inputSel = (inputSelector && String(inputSelector).trim()) || SELECTORS.otp.input;
  const submitSel = (submitSelector && String(submitSelector).trim()) || SELECTORS.otp.submit;
  const submitAfterFill = options.submitAfterFill !== false;
  const deadline = Date.now() + timeoutSec * 1000;
  const url = otpApiUrl + (otpApiUrl.indexOf('?') >= 0 ? '&' : '?') + 'account=' + encodeURIComponent(account);
  let lastCode = '';
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      const data = await res.json().catch(() => ({}));
      const code = (data && data.code) ? String(data.code).trim() : '';
      if (code !== '') {
        lastCode = code;
        const input = await findVisible(page, inputSel);
        if (input) {
          await typeLikeHuman(page, inputSel, code, { delayMin: 80, delayMax: 180 });
          if (!submitAfterFill) {
            return { ok: true, otp_result: 'ok' };
          }
          await clickVisible(page, submitSel);
          await page.waitForTimeout(3000);
          const err = await page.$(SELECTORS.otp.errorMsg);
          const errText = err ? await err.textContent().catch(() => '') : '';
          if (errText && (errText.indexOf('错误') >= 0 || errText.indexOf('invalid') >= 0)) {
            return { ok: false, otp_result: 'wrong' };
          }
          return { ok: true, otp_result: 'ok' };
        }
      }
    } catch (e) {
      // ignore
    }
    await page.waitForTimeout(pollIntervalSec * 1000);
  }
  return { ok: false, otp_result: 'timeout' };
}

/**
 * 检测当前页是否处于「等待短信验证码」状态（有验证码输入框）
 * @param {string} [selector] 输入框选择器，未传则用 SELECTORS.otp.input；先主页面再遍历 iframe
 */
async function hasOtpInput(page, selector) {
  const sel = (selector && String(selector).trim()) || SELECTORS.otp.input;
  let el = await page.$(sel);
  if (el) return true;
  for (const frame of page.frames()) {
    try {
      el = await frame.$(sel);
      if (el) return true;
    } catch (e) {
      // ignore this frame
    }
  }
  return false;
}

/**
 * 在登录页填写并提交；若出现验证码页则轮询 PHP 等待人工录入后填入
 * 拟人化：仅操作可见元素、逐字输入、随机延迟、点击前停顿
 */
async function loginKbiz(page, username, password, options = {}) {
  try {
    await page.waitForSelector(SELECTORS.login.username, { timeout: 15000 });
    await randomDelay(500, 1500);
    const userEl = await findVisible(page, SELECTORS.login.username);
    if (!userEl) return { success: false, error: '未找到用户名/账号输入框' };
    await userEl.click();
    await randomDelay(100, 300);
    await typeLikeHuman(page, SELECTORS.login.username, username);
    await randomDelay(300, 700);
    await typeLikeHuman(page, SELECTORS.login.password, password);
    await randomDelay(500, 1200);
    const submitted = await clickVisible(page, SELECTORS.login.submit, { beforeClickMin: 600, beforeClickMax: 1400 });
    if (!submitted) return { success: false, error: '未找到登录按钮' };
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
    await randomDelay(800, 2000);
    if (await hasOtpInput(page) && options.otp_api_url) {
      const timeout = options.otp_wait_timeout_sec != null ? options.otp_wait_timeout_sec : 180;
      const interval = options.otp_poll_interval_sec != null ? options.otp_poll_interval_sec : 3;
      const otpResult = await waitForOtp(page, username, options.otp_api_url, timeout, interval);
      if (!otpResult.ok) {
        return { success: false, otp_result: otpResult.otp_result, error: otpResult.otp_result === 'wrong' ? '验证码错误' : '等待验证码超时' };
      }
      await page.waitForTimeout(2000);
    }
    const url = page.url();
    if (url.indexOf('authen') !== -1 && url.indexOf('authen') === url.lastIndexOf('authen')) {
      return { success: false, error: '提交后仍在认证/登录页' };
    }
    return true;
  } catch (e) {
    return { success: false, error: '登录异常: ' + (e.message || String(e)) };
  }
}

/**
 * 读取已查过的收款缓存
 */
function readReceiptCache(cachePath) {
  try {
    const dir = path.dirname(cachePath);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    if (!fs.existsSync(cachePath)) return [];
    const raw = fs.readFileSync(cachePath, 'utf8');
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : (data.ids || []);
  } catch (e) {
    return [];
  }
}

/**
 * 写入已查过的收款 ID 集合（追加）
 */
function writeReceiptCache(cachePath, newIds) {
  try {
    const existing = readReceiptCache(cachePath);
    const set = new Set(existing);
    newIds.forEach(id => set.add(id));
    const dir = path.dirname(cachePath);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(cachePath, JSON.stringify(Array.from(set), null, 2));
  } catch (e) {
    // ignore
  }
}

/**
 * 查看收款记录：进入收款列表，用 AI 返回的解析规范 + 通道 receive_params 从 DOM 读取每笔的时间、金额等；已查过的（在缓存中）则跳过
 * options: { receipt_parse_url?: string, channelid?: number, account_key?: string, receive_params?: object }
 */
async function viewReceipts(page, cachePath, options = {}) {
  const cache = readReceiptCache(cachePath);
  const cacheSet = new Set(cache);

  const receiveParams = options.receive_params && typeof options.receive_params === 'object' ? options.receive_params : null;

  // 永远只使用数据库 receive_params；未配置则视为配置错误（不回退使用文档/AI spec）
  const dbRowSelector = receiveParams && typeof receiveParams.row_selector === 'string' ? receiveParams.row_selector.trim() : '';
  const dbAmountSelector = receiveParams && typeof receiveParams.amount_selector === 'string' ? receiveParams.amount_selector.trim() : '';
  if (!dbRowSelector || !dbAmountSelector) {
    return {
      success: false,
      receipts: [],
      checked_receipts: [],
      summary: 'Parse receipts error: missing receive_params.row_selector/amount_selector',
      new_count: 0,
      error: 'Parse receipts error: missing receive_params.row_selector/amount_selector',
    };
  }

  try {
    await randomDelay(400, 1000);
    const link = await findVisible(page, SELECTORS.receipts.menuLink);
    if (link) {
      await randomDelay(300, 800);
      await link.click();
      await page.waitForTimeout(2000);
      await randomDelay(500, 1200);
    }
  } catch (e) {
    return {
      success: true,
      receipts: [],
      checked_receipts: [],
      summary: 'No receipts menu or page not found',
      new_count: 0,
    };
  }

  // 用 receive_params.row_selector 在所有 frame 中定位“结果列表”所在 frame
  const frames = page.frames();
  let targetFrame = page.mainFrame();
  let bestCount = -1;
  const fastEnoughCount = 1; // 命中至少 1 行即可认为很可能找对
  for (const frame of frames) {
    try {
      // 轻量探测：只统计行数，不做全量解析
      const rows = await frame.$$(dbRowSelector);
      const count = rows ? rows.length : 0;
      if (count > bestCount) {
        bestCount = count;
        targetFrame = frame;
        if (bestCount >= fastEnoughCount) {
          // 短路：已经命中行，通常无需再遍历更多 frame
          break;
        }
      }
    } catch (e) {
      // ignore this frame
    }
  }

  let htmlFragment = '';
  try {
    htmlFragment = await targetFrame.content();
    if (htmlFragment.length > 50000) {
      htmlFragment = htmlFragment.slice(0, 50000);
    }
  } catch (e) {
    htmlFragment = '';
  }

  // 永远只使用数据库 receive_params；未配置则视为配置错误（不回退使用文档/AI spec）
  const spec = {
    row_selector: dbRowSelector,
    time_selector: (receiveParams && receiveParams.date_selector) ? String(receiveParams.date_selector).trim() : '',
    amount_selector: dbAmountSelector,
    row_id_selector: (receiveParams && receiveParams.row_id_selector) ? String(receiveParams.row_id_selector).trim() : '',
    filters: { amount_must_have_digit: true },
  };

  const receipts = [];
  const checked = [];
  try {
    const rows = await targetFrame.$$(spec.row_selector);
    const filters = spec.filters && typeof spec.filters === 'object' ? spec.filters : {};
    const requireDigit = filters.amount_must_have_digit !== false; // 默认需要金额中包含数字

    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      if (!(await row.isVisible())) continue;

      // 先解析时间、金额、对方账号（用于唯一 rowId 和后续提交）
      let time = '';
      if (spec.time_selector) {
        const timeEl = await row.$(spec.time_selector);
        time = timeEl ? ((await timeEl.textContent()) || '').trim() : '';
      } else {
        const timeEl = await row.$(SELECTORS.receipts.timeCell);
        time = timeEl ? ((await timeEl.textContent()) || '').trim() : '';
      }

      const amountEl = await row.$(spec.amount_selector);
      let amount = amountEl ? ((await amountEl.textContent()) || '').trim().replace(/\s+/g, '') : '';
      // 仅当疑似「脚本+文本」重复拼接（如 5.005.00）时才取第一段金额，其它站点不动
      if (amount && /\d+\.\d{2}\d/.test(amount)) {
        const first = amount.match(/(\d+\.\d{2})/)?.[1] || amount.match(/(\d+\.?\d*)/)?.[1];
        if (first) amount = first;
      }

      if (receiveParams) {
        if (receiveParams.amount_selector) {
          const aEl = await row.$(receiveParams.amount_selector);
          let aText = aEl ? ((await aEl.textContent()) || '').trim() : '';
          if (receiveParams.amount_parse === 'number') {
            aText = aText.replace(/[^\d.\-]/g, '');
            // 仅当疑似重复拼接（如 5.005.00）时取第一段，其它 DOM 不处理
            if (/\d+\.\d{2}\d/.test(aText)) {
              const firstAmount = aText.match(/(\d+\.\d{2})/)?.[1] || aText.match(/(\d+\.?\d*)/)?.[1];
              if (firstAmount) aText = firstAmount;
            }
          }
          if (aText !== '') amount = aText;
        }
        if (receiveParams.date_selector) {
          const dEl = await row.$(receiveParams.date_selector);
          let dText = dEl ? ((await dEl.textContent()) || '').trim().replace(/\s+/g, '') : '';
          if (receiveParams.date_parse && receiveParams.date_parse.type === 'ccb_td2') {
            const digits = dText.replace(/[^\d]/g, '');
            if (digits.length >= 14) {
              time = `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)} ${digits.slice(8, 10)}:${digits.slice(10, 12)}:${digits.slice(12, 14)}`;
            } else {
              time = dText;
            }
          } else {
            time = dText;
          }
        }
      }

      if (requireDigit && amount && !/\d/.test(amount)) continue;

      let returnOrderID = '';
      if (receiveParams && receiveParams.returnOrderID_selector) {
        const rEl = await row.$(receiveParams.returnOrderID_selector);
        returnOrderID = rEl ? ((await rEl.textContent()) || '').trim() : '';
      }

      // 缓存去重：用「时间+金额+对方账号」拼成唯一 id，避免同一天多笔重复/误判
      let rowId = `row_${i}`;
      if (receiveParams && (time || amount || returnOrderID)) {
        rowId = [time, amount, returnOrderID].filter(Boolean).join('|');
      } else if (spec.row_id_selector) {
        const idEl = await row.$(spec.row_id_selector);
        if (idEl) {
          const attrId = await idEl.getAttribute('data-id');
          const textId = (await idEl.textContent()) || '';
          rowId = (attrId && attrId.trim()) || textId.trim() || rowId;
        }
      } else {
        const idEl = await row.$(SELECTORS.receipts.rowId);
        if (idEl) {
          const attrId = await idEl.getAttribute('data-id');
          const textId = (await idEl.textContent()) || '';
          rowId = (attrId && attrId.trim()) || textId.trim() || rowId;
        }
      }
      if (cacheSet.has(rowId)) continue;

      const rec = { id: rowId, received_at: time, amount };
      if (returnOrderID) rec.returnOrderID = returnOrderID;
      receipts.push(rec);
      checked.push(rowId);
    }
  } catch (e) {
    return {
      success: false,
      receipts: [],
      checked_receipts: [],
      summary: 'Parse receipts error: ' + (e.message || ''),
      new_count: 0,
      error: 'Parse receipts error: ' + (e.message || ''),
    };
  }

  if (checked.length > 0) {
    writeReceiptCache(cachePath, checked);
  }

  return {
    success: true,
    receipts,
    checked_receipts: receipts,
    summary: 'Viewed ' + receipts.length + ' new receipt(s)',
    new_count: receipts.length,
  };
}

/**
 * 图片验证码：区域截图 → OCR 识别，失败则 POST 到 PHP AI 兜底
 * @returns {Promise<string>} 5~6 位数字或空
 */
async function recognizeImageCaptcha(page, captchaBoxSelector, captchaApiUrl, options = {}) {
  const logFn = typeof options.logFn === 'function' ? options.logFn : () => {};
  const tag = options.tag ? String(options.tag) : '';
  const expectedLength = Number.isInteger(options.expectedLength) && options.expectedLength > 0
    ? options.expectedLength
    : 0;
  const colorHint = options.colorHint ? String(options.colorHint).trim() : '';
  const sel = captchaBoxSelector || SELECTORS.captcha.box;
  let box = options.box || null;
  const reusedBox = !!box;
  const pageBox = reusedBox ? null : await page.$(sel);
  if (!box) box = pageBox;
  // 兜底：page.$ 只查主页面，若验证码在 iframe 中则改用 findVisible
  if (!box) box = await findVisible(page, sel);
  logFn('[kbiz.captcha.recognize] ' + tag + ' selector="' + sel + '" reused_box=' + reusedBox + ' page_box_found=' + !!pageBox + ' final_box_found=' + !!box);
  if (!box) return '';
  let buf = null;
  try {
    const src = await box.getAttribute('src').catch(() => '');
    logFn('[kbiz.captcha.recognize] ' + tag + ' img_src_present=' + !!src + (src ? ' img_src_sample=' + String(src).slice(0, 120) : ''));
  } catch (e) {
    // ignore
  }
  try {
    buf = await box.screenshot();
    logFn('[kbiz.captcha.recognize] ' + tag + ' screenshot_ok=true bytes=' + (buf ? buf.length : 0));
  } catch (e) {
    logFn('[kbiz.captcha.recognize] ' + tag + ' screenshot_ok=false error=' + (e && e.message ? e.message : String(e)));
    return '';
  }
  // 按用户要求：Node 不做图像处理，直接提交页面截取到的验证码原图
  const base64 = buf.toString('base64');
  if (!captchaApiUrl) {
    logFn('[kbiz.captcha.recognize] ' + tag + ' ocr_remote_error=missing_captcha_api_url');
    return '';
  }
  try {
    const bodyParts = [
      'image_base64=' + encodeURIComponent(base64),
    ];
    if (expectedLength > 0) bodyParts.push('expected_length=' + encodeURIComponent(String(expectedLength)));
    if (colorHint) bodyParts.push('color_hint=' + encodeURIComponent(colorHint));
    const res = await fetch(captchaApiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: bodyParts.join('&'),
    });
    const bodyText = await res.text().catch(() => '');
    if (!res.ok) {
      logFn('[kbiz.captcha.recognize] ' + tag
        + ' ocr_remote_status=' + res.status
        + ' ocr_remote_error_body=' + String(bodyText || '').slice(0, 800));
      return AI_HARD_ERROR_TOKEN;
    }
    let data = {};
    try {
      data = bodyText ? JSON.parse(bodyText) : {};
    } catch (e) {
      data = {};
    }
    const remoteRaw = (data && data.code != null)
      ? String(data.code)
      : (data && data.data && data.data.code != null ? String(data.data.code) : '');
    const remoteDigits = normalizeCaptchaDigits(remoteRaw, { expectedLength });
    logFn('[kbiz.captcha.recognize] ' + tag
      + ' expected_length=' + (expectedLength || 0)
      + ' ocr_remote_status=' + res.status
      + ' ocr_remote_body_sample=' + String(bodyText || '').slice(0, 180)
      + ' ocr_remote_digits_raw="' + (remoteRaw || '')
      + '" ocr_remote_digits_norm="' + (remoteDigits || '') + '"');
    if (remoteDigits) {
      logFn('[kbiz.captcha.recognize] ' + tag + ' ocr_chosen_source=remote ocr_chosen_digits="' + remoteDigits + '"');
      return remoteDigits;
    }
  } catch (e) {
    logFn('[kbiz.captcha.recognize] ' + tag + ' ocr_remote_error=' + (e && e.message ? e.message : String(e)));
  }
  logFn('[kbiz.captcha.recognize] ' + tag + ' ocr_chosen_source=none ocr_chosen_digits=""');
  return '';
}

/**
 * 使用 tesseract.js 从图片 buffer 识别数字（仅保留数字）
 */
async function ocrDigitsFromImageBuffer(buffer) {
  const OCR_OPTIONS = [
    { logger: () => {}, psm: 7, tessedit_char_whitelist: '0123456789' },
    { logger: () => {} },
  ];
  try {
    const Tesseract = require('tesseract.js');
    for (const opts of OCR_OPTIONS) {
      const { data } = await Tesseract.recognize(buffer, 'eng', opts);
      const text = (data && data.text) ? data.text : '';
      const digits = text.replace(/\D/g, '');
      if (digits) return digits;
    }
    return '';
  } catch (e) {
    return '';
  }
}

/**
 * 对验证码区域进行二次裁剪：优先截取右侧数字区域，避免左侧账号/金额干扰。
 * 当前建行附加码通常位于图片右半区域，保留中间高度。
 */
async function captureCaptchaFocusedBuffer(page, box) {
  const rect = await box.boundingBox();
  if (!rect || !rect.width || !rect.height) return null;
  const clip = {
    // 更激进地收敛到右侧，减少左侧账号/金额数字干扰
    x: Math.max(0, rect.x + rect.width * 0.56),
    y: Math.max(0, rect.y + rect.height * 0.07),
    width: Math.max(1, rect.width * 0.41),
    height: Math.max(1, rect.height * 0.86),
  };
  return await page.screenshot({ type: 'png', clip });
}

/**
 * 在页面内用 canvas 做颜色过滤：尽量去掉黑/灰干扰线与文字，仅保留彩色验证码笔画。
 * 输出为黑字白底的右侧验证码区域，便于 OCR。
 */
async function captureCaptchaColorMaskBuffer(box) {
  const b64 = await box.evaluate((el) => {
    const target = (el && el.tagName && String(el.tagName).toLowerCase() === 'img')
      ? el
      : (el && el.querySelector ? el.querySelector('img') : null);
    if (!target) return '';
    const w = Number(target.naturalWidth || target.width || 0);
    const h = Number(target.naturalHeight || target.height || 0);
    if (!w || !h) return '';

    const srcCanvas = document.createElement('canvas');
    srcCanvas.width = w;
    srcCanvas.height = h;
    const srcCtx = srcCanvas.getContext('2d');
    srcCtx.drawImage(target, 0, 0, w, h);

    let imgData;
    try {
      imgData = srcCtx.getImageData(0, 0, w, h);
    } catch (e) {
      // 跨域污染时 getImageData 会报错
      return '';
    }

    const data = imgData.data;
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const sat = max === 0 ? 0 : (max - min) / max;
      const val = max / 255;
      const isGray = Math.abs(r - g) < 22 && Math.abs(g - b) < 22 && Math.abs(r - b) < 22;
      const keepColor = sat >= 0.24 && val >= 0.18 && !(isGray && val < 0.78);

      if (keepColor) {
        // OCR 友好：保留目标像素为黑色
        data[i] = 0;
        data[i + 1] = 0;
        data[i + 2] = 0;
        data[i + 3] = 255;
      } else {
        data[i] = 255;
        data[i + 1] = 255;
        data[i + 2] = 255;
        data[i + 3] = 255;
      }
    }
    srcCtx.putImageData(imgData, 0, 0);

    // 验证码通常位于图片右侧，裁剪右侧区域减少左侧账号/金额干扰
    const cropX = Math.max(0, Math.floor(w * 0.55));
    const cropY = Math.max(0, Math.floor(h * 0.05));
    const cropW = Math.max(1, Math.floor(w * 0.43));
    const cropH = Math.max(1, Math.floor(h * 0.90));
    const outCanvas = document.createElement('canvas');
    outCanvas.width = cropW;
    outCanvas.height = cropH;
    const outCtx = outCanvas.getContext('2d');
    outCtx.fillStyle = '#fff';
    outCtx.fillRect(0, 0, cropW, cropH);
    outCtx.drawImage(srcCanvas, cropX, cropY, cropW, cropH, 0, 0, cropW, cropH);

    return outCanvas.toDataURL('image/png').replace(/^data:image\/png;base64,/, '');
  });
  if (!b64) return null;
  try {
    return Buffer.from(b64, 'base64');
  } catch (e) {
    return null;
  }
}

/**
 * 将 OCR 原始数字清洗为验证码。
 * - 若传入 expectedLength（如 5），则只接受该位数；
 * - 否则默认在 4/5/6 位中选候选。
 */
function normalizeCaptchaDigits(raw, options = {}) {
  const expectedLength = Number.isInteger(options.expectedLength) && options.expectedLength > 0
    ? options.expectedLength
    : 0;
  const digits = String(raw || '').replace(/\D/g, '');
  if (!digits) return '';
  if (expectedLength > 0 && new RegExp('^\\d{' + expectedLength + '}$').test(digits)) {
    if (/^(\d)\1+$/.test(digits)) return '';
    return digits;
  }
  if (expectedLength === 0 && /^\d{4,6}$/.test(digits)) {
    if (/^(\d)\1+$/.test(digits)) return '';
    return digits;
  }
  const candidates = [];
  const pushCandidates = (len) => {
    if (digits.length < len) return;
    for (let i = 0; i <= digits.length - len; i++) {
      const c = digits.slice(i, i + len);
      if (/^(\d)\1+$/.test(c)) continue;
      const uniq = new Set(c.split('')).size;
      candidates.push({ c, uniq, i, len });
    }
  };
  if (expectedLength > 0) {
    pushCandidates(expectedLength);
  } else {
    // 未指定位数时，优先 5 位（本通道实测更常见），其次 4/6 位
    pushCandidates(5);
    pushCandidates(4);
    pushCandidates(6);
  }
  if (!candidates.length) return '';
  candidates.sort((a, b) => {
    if (b.uniq !== a.uniq) return b.uniq - a.uniq;
    if (b.len !== a.len) return b.len - a.len;
    return a.i - b.i;
  });
  return candidates[0].c;
}

function parseChineseNumberWord(word) {
  const s = String(word || '').trim();
  if (!s) return 0;
  const map = { 零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  if (s === '十') return 10;
  if (s.length === 1 && map[s] != null) return map[s];
  if (s.indexOf('十') >= 0) {
    const parts = s.split('十');
    const left = parts[0] ? (map[parts[0]] != null ? map[parts[0]] : 0) : 1;
    const right = parts[1] ? (map[parts[1]] != null ? map[parts[1]] : 0) : 0;
    return left * 10 + right;
  }
  return 0;
}

function parseCaptchaHintText(text) {
  const raw = String(text || '').replace(/\s+/g, ' ').trim();
  if (!raw) return { expectedLength: 0, colorHint: '', raw: '' };
  let expectedLength = 0;
  const mDigit = raw.match(/(\d{1,2})\s*位/);
  if (mDigit) {
    expectedLength = Number(mDigit[1]) || 0;
  } else {
    const mCn = raw.match(/([零一二两三四五六七八九十]{1,3})\s*位/);
    if (mCn) expectedLength = parseChineseNumberWord(mCn[1]);
  }
  expectedLength = expectedLength > 0 ? Math.max(1, Math.min(8, expectedLength)) : 0;

  let colorHint = '';
  const mColor = raw.match(/(红色|绿色|蓝色|黄色|橙色|紫色|黑色|灰色|红|绿|蓝|黄|橙|紫|黑|灰)\s*数字/);
  if (mColor) {
    const c = mColor[1];
    colorHint = c.endsWith('色') ? c : (c + '色');
  }
  return { expectedLength, colorHint, raw };
}

async function resolveCaptchaHints(page, pp, fallbackLength, fallbackColorHint, logFn, tag = '') {
  let expectedLength = fallbackLength || 0;
  let colorHint = fallbackColorHint || '';
  const hintSelector = (pp && pp.captcha_hint_selector && String(pp.captcha_hint_selector).trim())
    ? String(pp.captcha_hint_selector).trim()
    : '';
  if (!hintSelector) {
    if (typeof logFn === 'function') {
      logFn('[kbiz.doOnePayment] '
        + tag
        + ' captcha_hint_selector=empty'
        + ' effective_length=' + (expectedLength || 0)
        + ' effective_color=' + (colorHint || ''));
    }
    return { expectedLength, colorHint, hintText: '' };
  }
  const hintEl = await findVisible(page, hintSelector);
  let hintText = '';
  if (hintEl) {
    hintText = await hintEl.evaluate((el) => (el && el.textContent) ? String(el.textContent) : '').catch(() => '');
  }
  const parsed = parseCaptchaHintText(hintText);
  if (parsed.expectedLength > 0) expectedLength = parsed.expectedLength;
  if (parsed.colorHint) colorHint = parsed.colorHint;
  if (typeof logFn === 'function') {
    logFn('[kbiz.doOnePayment] '
      + tag
      + ' captcha_hint_selector="' + hintSelector + '"'
      + ' hint_text="' + (parsed.raw || '').slice(0, 180) + '"'
      + ' effective_length=' + (expectedLength || 0)
      + ' effective_color=' + (colorHint || ''));
  }
  return { expectedLength, colorHint, hintText: parsed.raw || '' };
}

/**
 * 执行付款：仅使用 payment_params 选择器，无硬编码。支持多订单循环（方案 A）。
 * 必填：options.payment_params 且含 amount_selector、submit_selector、payee_selector 或 bankcard_selector 至少一个。
 * 若 options.orders 存在且非空，则按订单循环；否则按单笔 amount/payee/memo 执行一次。
 */
async function executePayment(page, payload, cachePath, options = {}) {
  const pp = options.payment_params || options.pay_params;
  if (!pp || typeof pp !== 'object') {
    return { success: false, error: '缺少 payment_params，无法执行付款' };
  }
  const amountSel = (pp.amount_selector && String(pp.amount_selector).trim()) || '';
  const submitSel = (pp.submit_selector && String(pp.submit_selector).trim()) || '';
  const payeeSel = (pp.payee_selector && String(pp.payee_selector).trim()) || '';
  const bankcardSel = (pp.bankcard_selector && String(pp.bankcard_selector).trim()) || '';
  if (!amountSel || !submitSel) {
    return { success: false, error: 'payment_params 缺少 amount_selector 或 submit_selector' };
  }
  if (!payeeSel && !bankcardSel) {
    return { success: false, error: 'payment_params 缺少 payee_selector 或 bankcard_selector' };
  }
  const payeeFieldSel = payeeSel || bankcardSel;

  const orders = Array.isArray(options.orders) ? options.orders : [];
  const isMultiOrder = orders.length > 0;
  const orderResults = [];
  const logFn = typeof options.logFn === 'function' ? options.logFn : () => {};
  const parsedCaptchaLength = Number(
    pp.captcha_length != null ? pp.captcha_length : (options.captcha_length != null ? options.captcha_length : 0),
  );
  const captchaLength = Number.isFinite(parsedCaptchaLength) && parsedCaptchaLength > 0
    ? Math.max(1, Math.min(8, Math.floor(parsedCaptchaLength)))
    : 0;
  const captchaColorHint = (pp.captcha_color_hint != null && String(pp.captcha_color_hint).trim())
    ? String(pp.captcha_color_hint).trim()
    : ((options.captcha_color_hint != null && String(options.captcha_color_hint).trim()) ? String(options.captcha_color_hint).trim() : '');
  logFn('[kbiz.executePayment] payment_params=ok orders_count=' + orders.length + ' isMultiOrder=' + isMultiOrder);

  async function doOnePayment(order) {
    const amount = order && order.mum != null ? String(order.mum) : (payload && payload.amount != null ? String(payload.amount) : '');
    const payee = order && order.bankcard != null ? String(order.bankcard) : (payload && payload.payee != null ? String(payload.payee) : '');
    const truename = order && order.truename != null ? String(order.truename) : '';
    const bank = order && order.bank != null ? String(order.bank) : '';
    const memo = order && order.memo != null ? String(order.memo) : (payload && payload.memo != null ? String(payload.memo) : '');
    const orderid = order && order.orderid != null ? order.orderid : '';
    logFn('[kbiz.doOnePayment] orderid=' + orderid + ' amount=' + (order && order.mum != null ? order.mum : (payload && payload.amount)));

    if (pp.menu_link_selector && String(pp.menu_link_selector).trim()) {
      try {
        await randomDelay(400, 1000);
        const link = await findVisible(page, String(pp.menu_link_selector).trim());
        if (!link) {
          logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=Payment menu not found');
          return { orderid, success: false, error: 'Payment menu not found' };
        }
        await randomDelay(300, 800);
        await link.click();
        await page.waitForTimeout(2000);
        await randomDelay(500, 1200);
      } catch (e) {
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=Navigate failed ' + (e.message || ''));
        return { orderid, success: false, error: 'Navigate to payment failed: ' + (e.message || '') };
      }
    }

    try {
      await typeLikeHuman(page, amountSel, amount);
      await randomDelay(300, 700);
      await typeLikeHuman(page, payeeFieldSel, payee, { forceClick: true });
      if (memo && pp.memo_selector && String(pp.memo_selector).trim()) {
        await randomDelay(200, 500);
        await typeLikeHuman(page, String(pp.memo_selector).trim(), memo, { forceClick: true });
      }
      if (pp.truename_selector && String(pp.truename_selector).trim() && truename) {
        await randomDelay(200, 500);
        await typeLikeHuman(page, String(pp.truename_selector).trim(), truename, { forceClick: true });
      }
      if (pp.bank_selector && String(pp.bank_selector).trim() && bank) {
        await randomDelay(200, 500);
        await typeLikeHuman(page, String(pp.bank_selector).trim(), bank, { forceClick: true });
      }
      await randomDelay(500, 1200);
      const submitted = await clickVisible(page, submitSel, { beforeClickMin: 600, beforeClickMax: 1400 });
      if (!submitted) {
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=Submit button not visible');
        return { orderid, success: false, error: 'Submit button not visible or click failed' };
      }
      await page.waitForTimeout(3000);
    } catch (e) {
      logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=Fill or submit ' + (e.message || ''));
      return { orderid, success: false, error: 'Fill or submit failed: ' + (e.message || '') };
    }

    const captchaInputSel = (pp.captcha_input_selector && String(pp.captcha_input_selector).trim()) || options.captcha_input_selector || SELECTORS.captcha.input;
    const captchaBoxSel = (pp.captcha_box_selector && String(pp.captcha_box_selector).trim()) || options.captcha_box_selector || SELECTORS.captcha.box;
    const otpInputSel = SELECTORS.otp.input;
    const waitStepMs = 500;
    const waitTimeoutMs = 12000;
    for (let elapsed = 0; elapsed < waitTimeoutMs; elapsed += waitStepMs) {
      const otpEl = await findVisible(page, otpInputSel);
      const captchaEl = await findVisible(page, captchaInputSel);
      if (otpEl || captchaEl) break;
      await page.waitForTimeout(waitStepMs);
    }
    const hasOtp = (await hasOtpInput(page)) && options.otp_api_url && options.username;
    const hasCaptchaOnPage = !!await findVisible(page, captchaInputSel);
    const resolvedHints = await resolveCaptchaHints(page, pp, captchaLength, captchaColorHint, logFn, 'orderid=' + orderid + ' stage=pre_detect');
    const runtimeCaptchaLength = resolvedHints.expectedLength || captchaLength || 0;
    const runtimeCaptchaColorHint = resolvedHints.colorHint || captchaColorHint || '';
    logFn('[kbiz.doOnePayment] orderid=' + orderid + ' detect hasOtp=' + !!hasOtp + ' hasCaptchaInput=' + hasCaptchaOnPage + ' otpInputSel="' + otpInputSel + '" captchaInputSel="' + captchaInputSel + '" captchaBoxSel="' + captchaBoxSel + '"');
    let alreadyClickedConfirm = false;

    if (hasOtp && hasCaptchaOnPage) {
      const timeout = options.otp_wait_timeout_sec != null ? options.otp_wait_timeout_sec : 180;
      const interval = options.otp_poll_interval_sec != null ? options.otp_poll_interval_sec : 3;
      const otpResult = await waitForOtp(page, options.username, options.otp_api_url, timeout, interval, undefined, undefined, { submitAfterFill: false });
      if (!otpResult.ok) {
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=OTP ' + (otpResult.otp_result || ''));
        return {
          orderid,
          success: false,
          error: otpResult.otp_result === 'wrong' ? '验证码错误' : '等待验证码超时',
        };
      }
      let code = '';
      const maxCaptchaRetriesWithOtp = 3;
      for (let retry = 0; retry < maxCaptchaRetriesWithOtp; retry++) {
        const hintInRetry = await resolveCaptchaHints(
          page,
          pp,
          runtimeCaptchaLength,
          runtimeCaptchaColorHint,
          logFn,
          'orderid=' + orderid + ' stage=otp_and_captcha retry=' + retry,
        );
        const box = await findVisible(page, captchaBoxSel);
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' retry=' + retry + ' detect captchaBox_visible=' + !!box + ' before_recognize_stage=otp_and_captcha');
        code = box ? await recognizeImageCaptcha(
          page,
          captchaBoxSel,
          options.captcha_api_url,
          {
            logFn,
            tag: 'orderid=' + orderid + ' stage=otp_and_captcha retry=' + retry,
            box,
            expectedLength: hintInRetry.expectedLength || runtimeCaptchaLength,
            colorHint: hintInRetry.colorHint || runtimeCaptchaColorHint,
          },
        ) : '';
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' retry=' + retry + ' captcha_code_len=' + (code ? String(code).length : 0) + ' stage=otp_and_captcha');
        if (code === AI_HARD_ERROR_TOKEN) {
          logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=AI请求失败, 停止刷新重试 stage=otp_and_captcha');
          return { orderid, success: false, error: '验证码识别失败' };
        }
        if (code) break;
        const refresh = await findVisible(page, SELECTORS.captcha.refreshLink);
        if (refresh) {
          await randomDelay(300, 700);
          await refresh.click();
          await page.waitForTimeout(1500);
        } else {
          await page.waitForTimeout(1000);
        }
      }
      if (!code) {
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=图片验证码识别失败');
        return { orderid, success: false, error: '图片验证码识别失败' };
      }
      await typeLikeHuman(page, captchaInputSel, code, { delayMin: 60, delayMax: 120 });
      await randomDelay(400, 900);
      if (pp.confirm_selector && String(pp.confirm_selector).trim()) {
        await randomDelay(300, 700);
        const confirmClicked = await clickVisible(page, String(pp.confirm_selector).trim(), { beforeClickMin: 400, beforeClickMax: 1000 });
        if (confirmClicked) alreadyClickedConfirm = true;
        await page.waitForTimeout(2000);
      }
    } else if (hasOtp) {
      const timeout = options.otp_wait_timeout_sec != null ? options.otp_wait_timeout_sec : 180;
      const interval = options.otp_poll_interval_sec != null ? options.otp_poll_interval_sec : 3;
      const otpResult = await waitForOtp(page, options.username, options.otp_api_url, timeout, interval);
      if (!otpResult.ok) {
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=OTP ' + (otpResult.otp_result || ''));
        return {
          orderid,
          success: false,
          error: otpResult.otp_result === 'wrong' ? '验证码错误' : '等待验证码超时',
        };
      }
      await page.waitForTimeout(2000);
    }

    if (!alreadyClickedConfirm) {
      for (let w = 0; w < 12; w++) {
        await page.waitForTimeout(500);
        if (await findVisible(page, captchaInputSel)) break;
      }
      const maxCaptchaRetries = 3;
      for (let retry = 0; retry < maxCaptchaRetries; retry++) {
        const captchaInput = await findVisible(page, captchaInputSel);
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' retry=' + retry + ' captchaInput_visible=' + !!captchaInput + ' stage=confirm_before_click');
        if (captchaInput) {
          const box = await findVisible(page, captchaBoxSel);
          logFn('[kbiz.doOnePayment] orderid=' + orderid + ' retry=' + retry + ' captchaBox_visible=' + !!box + ' stage=confirm_before_click');
          const hint2 = await resolveCaptchaHints(page, pp, runtimeCaptchaLength, runtimeCaptchaColorHint, logFn, 'orderid=' + orderid + ' stage=confirm_before_click retry=' + retry);
          const code = box ? await recognizeImageCaptcha(page, captchaBoxSel, options.captcha_api_url, { logFn, tag: 'orderid=' + orderid + ' stage=confirm_before_click retry=' + retry, box, expectedLength: hint2.expectedLength || runtimeCaptchaLength, colorHint: hint2.colorHint || runtimeCaptchaColorHint }) : '';
          logFn('[kbiz.doOnePayment] orderid=' + orderid + ' retry=' + retry + ' captcha_code_len=' + (code ? String(code).length : 0) + ' stage=confirm_before_click');
          if (code === AI_HARD_ERROR_TOKEN) {
            logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=AI请求失败, 停止刷新重试 stage=confirm_before_click');
            return { orderid, success: false, error: '验证码识别失败' };
          }
          if (!code) {
            const refresh = await findVisible(page, SELECTORS.captcha.refreshLink);
            if (refresh) {
              await randomDelay(300, 700);
              await refresh.click();
              await page.waitForTimeout(1500);
              continue;
            }
            logFn('[kbiz.doOnePayment] orderid=' + orderid + ' error=图片验证码识别失败');
            return { orderid, success: false, error: '图片验证码识别失败' };
          }
          await typeLikeHuman(page, captchaInputSel, code, { delayMin: 60, delayMax: 120 });
          await randomDelay(400, 900);
          const stillCaptcha = await findVisible(page, captchaInputSel);
          if (stillCaptcha) {
            const refresh = await findVisible(page, SELECTORS.captcha.refreshLink);
            if (refresh) {
              await randomDelay(300, 700);
              await refresh.click();
              await page.waitForTimeout(1500);
            }
            continue;
          }
        }
        break;
      }

      if (pp.confirm_selector && String(pp.confirm_selector).trim()) {
        await randomDelay(300, 700);
        const confirmClicked = await clickVisible(page, String(pp.confirm_selector).trim(), { beforeClickMin: 400, beforeClickMax: 1000 });
        if (confirmClicked) await page.waitForTimeout(2000);
      }
    }

    const successAreaSel = pp.success_area_selector && String(pp.success_area_selector).trim();
    const failureAreaSel = (pp.failure_area_selector && String(pp.failure_area_selector).trim()) || '';
    const successContentSel = (pp.success_content_selector && String(pp.success_content_selector).trim()) || '';
    const successKeyword = (pp.success_keyword != null && String(pp.success_keyword).trim()) ? String(pp.success_keyword).trim() : '';
    const resultTimeoutMs = (pp.result_wait_timeout_ms != null && pp.result_wait_timeout_ms > 0) ? pp.result_wait_timeout_ms : 15000;
    const pollStepMs = 800;
    let successEl = null;
    let failureEl = null;
    for (let elapsed = 0; elapsed < resultTimeoutMs; elapsed += pollStepMs) {
      await page.waitForTimeout(pollStepMs);
      if (successAreaSel) successEl = await findVisible(page, successAreaSel);
      if (failureAreaSel) failureEl = await findVisible(page, failureAreaSel);
      if (successEl) {
        let isSuccessPage = false;
        if (successContentSel) {
          try {
            const inner = await successEl.$(successContentSel);
            isSuccessPage = !!(inner && (await inner.isVisible()));
          } catch (e) {
            // ignore
          }
        }
        if (!isSuccessPage && successKeyword) {
          const text = await successEl.evaluate((el) => (el && el.textContent) ? el.textContent : '').catch(() => '');
          isSuccessPage = (text && text.indexOf(successKeyword) !== -1);
        }
        if (isSuccessPage) break;
      }
      if (failureEl) break;
    }

    if (successEl) {
      let isSuccessPage = false;
      if (successContentSel) {
        try {
          const inner = await successEl.$(successContentSel);
          isSuccessPage = !!(inner && (await inner.isVisible()));
        } catch (e) {
          // ignore
        }
      }
      if (!isSuccessPage && successKeyword) {
        const text = await successEl.evaluate((el) => (el && el.textContent) ? el.textContent : '').catch(() => '');
        isSuccessPage = (text && text.indexOf(successKeyword) !== -1);
      }
      if (isSuccessPage) {
        let screenshotBase64 = '';
        try {
          const buf = await successEl.screenshot();
          screenshotBase64 = buf ? buf.toString('base64') : '';
        } catch (e) {
          const buf = await page.screenshot({ encoding: 'base64', type: 'png' });
          screenshotBase64 = buf || '';
        }
        logFn('[kbiz.doOnePayment] orderid=' + orderid + ' result=success screenshot_len=' + (screenshotBase64 ? screenshotBase64.length : 0));
        return { orderid, success: true, screenshot_base64: screenshotBase64 || undefined };
      }
    }

    let failMsg = '未检测到转账成功';
    if (failureEl) {
      try {
        const text = await failureEl.evaluate((el) => (el && el.textContent) ? el.textContent : '').catch(() => '');
        if (text && text.trim()) failMsg = text.trim().replace(/\s+/g, ' ').slice(0, 200);
      } catch (e) {
        // ignore
      }
    }
    logFn('[kbiz.doOnePayment] orderid=' + orderid + ' result=fail error=' + failMsg);
    return { orderid, success: false, error: failMsg };
  }

  if (isMultiOrder) {
    for (let i = 0; i < orders.length; i++) {
      const res = await doOnePayment(orders[i]);
      orderResults.push(res);
      logFn('[kbiz.executePayment] order_result orderid=' + (res.orderid || '') + ' success=' + !!res.success + (res.error ? ' error=' + res.error : ''));
      if (i < orders.length - 1) {
        const continueSel = pp.continue_transfer_selector && String(pp.continue_transfer_selector).trim();
        if (continueSel && res.success) {
          await randomDelay(500, 1000);
          const clicked = await clickVisible(page, continueSel, { beforeClickMin: 400, beforeClickMax: 900 });
          if (clicked) await page.waitForTimeout(2500);
        }
        await randomDelay(800, 1500);
      }
    }
    logFn('[kbiz.executePayment] multi_done total=' + orderResults.length + ' success_count=' + orderResults.filter((r) => r.success).length);
    return {
      success: orderResults.every((r) => r.success),
      order_results: orderResults,
      summary: 'Processed ' + orderResults.length + ' order(s)',
    };
  }

  const single = await doOnePayment(null);
  if (single.success) {
    return {
      success: true,
      payment_id: 'pay_' + Date.now(),
      amount: payload && payload.amount,
      payee: payload && payload.payee,
      memo: (payload && payload.memo) || '',
      summary: 'Payment submitted',
    };
  }
  return {
    success: false,
    error: single.error || 'Payment failed',
    order_results: [single],
  };
}

/**
 * 执行文档驱动下发的 browserSteps（fill/click），value 中 {{username}}/{{password}} 由 vars 替换
 */
async function runBrowserSteps(page, steps, vars = {}) {
  for (const step of steps || []) {
    const out = await runSingleStep(page, step, vars);
    if (!out.success) return out;
  }
  return { success: true };
}

/**
 * 执行文档驱动下发的单步（fill/click），value 中 {{username}}/{{password}} 由 vars 替换
 * @param {object} page Playwright page
 * @param {{ type: string, selector?: string, value?: string }} step
 * @param {{ username: string, password: string }} vars
 * @returns {Promise<{ success: boolean, error?: string }>}
 */
async function runSingleStep(page, step, vars = {}) {
  const username = vars.username || '';
  const password = vars.password || '';
  const today = vars.today || '';
  const substitute = (s) => {
    if (typeof s !== 'string') return s;
    return String(s)
      .replace(/\{\{username\}\}/g, username)
      .replace(/\{\{password\}\}/g, password)
      .replace(/\{\{today\}\}/g, today);
  };
  const type = (step.type || '').toLowerCase();
  const selector = typeof step.selector === 'string' ? step.selector.trim() : '';
  const optional = step.optional === true;
  if (type === 'fill') {
    if (!selector) return { success: false, error: 'fill 步骤缺少 selector' };
    const selectors = selector.split(',').map((s) => s.trim()).filter(Boolean);
    const value = substitute(step.value || '');
    const el = await findVisible(page, selectors.length ? selectors : selector);
    if (!el) {
      if (optional) {
        return { success: true, skipped: true };
      }
      return { success: false, error: '未找到输入框: ' + selector };
    }
    await el.click();
    await randomDelay(100, 300);
    await el.fill(value);
    await randomDelay(200, 500);
    return { success: true };
  }
  if (type === 'click') {
    if (!selector) return { success: false, error: 'click 步骤缺少 selector' };
    const selectors = selector.split(',').map((s) => s.trim()).filter(Boolean);
    const ok = await clickVisible(page, selectors.length ? selectors : selector, { beforeClickMin: 400, beforeClickMax: 1200 });
    if (!ok) {
      if (optional) {
        return { success: true, skipped: true };
      }
      return { success: false, error: '未找到可点击元素: ' + selector };
    }
    await page.waitForTimeout(1500);
    return { success: true };
  }
  return { success: false, error: 'Unknown step type: ' + type };
}

module.exports = {
  loginKbiz,
  viewReceipts,
  executePayment,
  readReceiptCache,
  writeReceiptCache,
  runBrowserSteps,
  runSingleStep,
  hasOtpInput,
  waitForOtp,
};
