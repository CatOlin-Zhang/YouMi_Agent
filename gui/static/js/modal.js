// 弹窗工具 — 通用的输入弹窗（新建群聊、添加成员、确认删除等）
//
// 依赖: initModal() 注入 DOM 引用后可使用。

let _modal = {};
let _pendingSubmit = null;

/** 注入弹窗相关 DOM 引用（由 ui.js 的 initUI 调用） */
export function initModal(els) {
  _modal = els;

  _modal.modalCancel.onclick = closeModal;
  _modal.modalOk.onclick = () => {
    if (_pendingSubmit) _pendingSubmit();
    closeModal();
  };
  _modal.modal.addEventListener("click", (e) => {
    if (e.target === _modal.modal) closeModal();
  });
}

/** 显示输入弹窗
 *
 * @param {string} title       弹窗标题
 * @param {Array}  fields      输入字段 [{ key, label, placeholder?, value? }]
 * @param {Function} onSubmit  提交回调，接收 { key: value } 对象
 * @param {string} okLabel     确认按钮文字（默认 "确定"）
 */
export function showModal(title, fields, onSubmit, okLabel) {
  _modal.modalTitle.textContent = title;
  _modal.modalBody.innerHTML = "";
  const inputs = {};
  for (const f of fields) {
    const label = document.createElement("label");
    label.textContent = f.label;
    const input = document.createElement("input");
    input.placeholder = f.placeholder || "";
    input.value = f.value || "";
    _modal.modalBody.appendChild(label);
    _modal.modalBody.appendChild(input);
    inputs[f.key] = input;
  }
  _pendingSubmit = () => {
    const vals = {};
    for (const k in inputs) vals[k] = inputs[k].value.trim();
    onSubmit(vals);
  };
  _modal.modalOk.textContent = okLabel || "确定";
  _modal.modal.classList.remove("hidden");
  const first = _modal.modalBody.querySelector("input");
  if (first) first.focus();
}

/** 关闭弹窗 */
export function closeModal() {
  _modal.modal.classList.add("hidden");
  _pendingSubmit = null;
}
