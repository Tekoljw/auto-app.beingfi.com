<?php
namespace Cli\Common;

/**
 * VMOS Cloud OpenAPI HMAC-SHA256 签名（与官方文档一致）
 * @see https://cloud.vmoscloud.com/vmoscloud/doc/zh/server/example.html
 */
class VmosCloudSigner {

    protected $accessKeyId;
    protected $secretAccessKey;
    protected $contentType = 'application/json;charset=UTF-8';
    protected $service = 'armcloud-paas';
    protected $algorithm = 'HMAC-SHA256';

    public function __construct($accessKeyId, $secretAccessKey) {
        $this->accessKeyId = (string)$accessKeyId;
        $this->secretAccessKey = (string)$secretAccessKey;
    }

    /**
     * POST JSON：请求体字符串须与 CURLOPT_POSTFIELDS 完全一致（同一 json_encode 结果）
     *
     * @param string $host           x-host，如 api.vmoscloud.com（与 VMOS_CLOUD_API_BASE 域名一致）
     * @param string $payloadString  已序列化的 JSON 字符串
     * @return array                 供 curl CURLOPT_HTTPHEADER 使用的字符串数组
     */
    public function signPost($host, $payloadString) {
        if ($payloadString === null) {
            $payloadString = '';
        }
        $xDate = gmdate('Ymd\THis\Z');
        $shortXDate = substr($xDate, 0, 8);
        $credentialScope = $shortXDate . '/' . $this->service . '/request';

        $xContentSha256 = hash('sha256', $payloadString);

        $canonicalString = implode("\n", [
            'host:' . $host,
            'x-date:' . $xDate,
            'content-type:' . $this->contentType,
            'signedHeaders:content-type;host;x-content-sha256;x-date',
            'x-content-sha256:' . $xContentSha256,
        ]);

        $hashedCanonicalString = hash('sha256', $canonicalString);

        $stringToSign = implode("\n", [
            $this->algorithm,
            $xDate,
            $credentialScope,
            $hashedCanonicalString,
        ]);

        $kDate = hash_hmac('sha256', $shortXDate, $this->secretAccessKey, true);
        $kService = hash_hmac('sha256', $this->service, $kDate, true);
        $signKey = hash_hmac('sha256', 'request', $kService, true);

        $signature = hash_hmac('sha256', $stringToSign, $signKey);

        $authorization = sprintf(
            '%s Credential=%s/%s, SignedHeaders=content-type;host;x-content-sha256;x-date, Signature=%s',
            $this->algorithm,
            $this->accessKeyId,
            $credentialScope,
            $signature
        );

        return [
            'x-date: ' . $xDate,
            'x-host: ' . $host,
            'authorization: ' . $authorization,
            'content-type: ' . $this->contentType,
        ];
    }
}
