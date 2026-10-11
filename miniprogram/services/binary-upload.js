// 微信开发者工具的隔离环境可能返回其他 realm 的 ArrayBuffer。
// 用原生 byteLength getter 校验真实字节，再复制到本环境交给 wx.request；
// instanceof 或可伪装的 toStringTag 都不能可靠地完成这个判断。
const bufferLength = Object.getOwnPropertyDescriptor(ArrayBuffer.prototype, "byteLength").get;

function normalizeBuffer(value, { limit, invalidMessage, oversizeMessage }) {
  let size;
  try { size = bufferLength.call(value); }
  catch (_) { throw new Error(invalidMessage); }
  if (!size) throw new Error(invalidMessage);
  if (size > limit) throw new Error(oversizeMessage);
  try {
    const bytes = new Uint8Array(size);
    bytes.set(new Uint8Array(value));
    return bytes.buffer;
  } catch (_) { throw new Error(invalidMessage); }
}

module.exports = { normalizeBuffer };
