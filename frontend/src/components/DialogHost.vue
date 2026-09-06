<script setup>
import { dialogState } from '../dialog'

const vFocus = { mounted: el => el.focus() }

function cancelValue(d) { return d.kind === 'prompt' ? null : false }
function okValue(d) { return d.kind === 'prompt' ? d.inputValue : true }
</script>

<template>
  <div v-for="d in dialogState.items" :key="d.id" class="mask dlg on" @click.self="d.close(cancelValue(d))">
    <div class="modal dlg-box">
      <h3>{{ d.title }}</h3>
      <div class="dlg-msg">{{ d.message }}</div>
      <div v-if="d.kind === 'prompt'" class="fld" style="margin-top:12px">
        <input v-model="d.inputValue" v-focus :type="d.inputType" :placeholder="d.placeholder"
          @keyup.enter="d.close(d.inputValue)" @keyup.esc="d.close(null)">
      </div>
      <div class="ft">
        <button v-if="d.kind !== 'alert'" class="btn" @click="d.close(cancelValue(d))">取消</button>
        <button class="btn pri" :class="{ danger: d.danger }" @click="d.close(okValue(d))">{{ d.okText || '确定' }}</button>
      </div>
    </div>
  </div>
  <div class="toast" :class="{ on: dialogState.toast.on }">{{ dialogState.toast.msg }}</div>
</template>
