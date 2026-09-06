// 全局弹窗：Promise 风格的 alert / confirm / prompt 与 toast，替代浏览器原生弹窗。
import { reactive } from 'vue'

export const dialogState = reactive({ items: [], toast: { msg: '', on: false } })
let seq = 0
let toastTimer = null

function open(opts) {
  return new Promise(resolve => {
    const item = reactive({ ...opts, id: ++seq, inputValue: opts.value ?? '' })
    item.close = v => {
      const i = dialogState.items.indexOf(item)
      if (i >= 0) dialogState.items.splice(i, 1)
      resolve(v)
    }
    dialogState.items.push(item)
  })
}

export function alertDialog(message, title = '提示') {
  return open({ kind: 'alert', title, message })
}

// confirmDialog(msg) / confirmDialog(msg, { danger: true }) —— danger 时确认按钮为红色
export function confirmDialog(message, { title = '确认操作', okText = '确定', danger = false } = {}) {
  return open({ kind: 'confirm', title, message, okText, danger })
}

// resolve 值：确认 → 输入内容（字符串，可为空串）；取消/关闭 → null
export function promptDialog(message, { title = '请输入', placeholder = '', value = '', inputType = 'text' } = {}) {
  return open({ kind: 'prompt', title, message, placeholder, value, inputType })
}

export function toast(msg, ms = 2200) {
  dialogState.toast.msg = msg
  dialogState.toast.on = true
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { dialogState.toast.on = false }, ms)
}
