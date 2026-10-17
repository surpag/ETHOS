// 模型目录变更广播。
//
// 为什么需要它：聊天页的可用模型列表在挂载时拉一次（/api/available-models），
// 而设置弹窗是覆盖在聊天页之上的 —— 用户在设置里加完模型、回到聊天页时
// 组件并没有重新挂载，列表还停在旧数据上。
// 轮询或每次渲染都拉都会带来无意义的请求，所以由写入方显式广播一次。

const MODELS_CHANGED_EVENT = "ethos:models-changed";

export function notifyModelsChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(MODELS_CHANGED_EVENT));
}

export function subscribeModelsChanged(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(MODELS_CHANGED_EVENT, handler);
  return () => window.removeEventListener(MODELS_CHANGED_EVENT, handler);
}
