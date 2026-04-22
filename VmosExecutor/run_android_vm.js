/**
 * 云机 APP 执行器（与 PwBrowse 无关）：由 AutoVmosExchangeController 通过 node 调用。
 * 接收 JSON 参数，最后一行 stdout 为结果 JSON。
 * 完整实现需对接 VMOS asyncCmd/ADB，并按 get_next_step_url / report_result_url 与 PHP 联调。
 */
'use strict';

function outputResult(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function main() {
  let input = {};
  const arg = process.argv[2];
  if (arg) {
    try {
      input = JSON.parse(arg);
    } catch (e) {
      outputResult({ success: false, error: 'Invalid JSON input: ' + e.message });
      process.exit(1);
      return;
    }
  }
  const action = input.action || 'viewReceipts';
  outputResult({
    success: false,
    error: 'run_android_vm.js 占位：请实现 ADB/文档驱动步骤（与 AutoVmosExchange getNextStep 联调）',
    action,
    pad_code: input.pad_code || '',
    doc_driven: !!input.doc_driven,
  });
}

main();
