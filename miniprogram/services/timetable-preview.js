// Presentation only: keep assistant replies readable without changing saved data.
const IANA_ZONE = "(?:(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc|US|Canada|Mexico|Brazil|Chile|SystemV|posix|right)(?:\\s*/\\s*[A-Za-z0-9_+.-]+)+|Asis\\s*/\\s*Shanghai)";
const OFFSET_ZONE = "(?:UTC|GMT)\\s*[+-]\\s*\\d{1,2}(?::?\\d{2})?";
const NAMED_ZONE = "(?:北京时间|中国标准时间|上海时间)";
function displayText(value) {
  return String(value || "")
    .replace(new RegExp(`(?:当前|默认|采用|使用|账户)?(?:时区|timezone)\\s*(?:[:：=]|为|是|设置为)?\\s*(?:${IANA_ZONE}|${OFFSET_ZONE}|${NAMED_ZONE}|(?:UTC|GMT|[A-Z]{2,5})\\b)[；;，,。]?\\s*`, "gi"), "")
    .replace(new RegExp(`(?:默认)?(?:${IANA_ZONE}|${OFFSET_ZONE}|${NAMED_ZONE})`, "gi"), "")
    .replace(/(^|[^A-Za-z0-9_\u3400-\u9fff])(?:UTC|GMT)(?![A-Za-z0-9_\u3400-\u9fff])/gi, "$1")
    .replace(/[（(]\s*[）)]/g, "")
    .replace(/[ \t]{2,}/g, " ").trim()
    .replace(/^[；;，,]\s*/, "");
}

module.exports = { displayText };
