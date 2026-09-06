<script setup>
import { ref } from 'vue'

const props = defineProps({
  roles: Array,       // [{key, name}]
  steps: Array,       // 流程步骤
  detail: Array,      // 可选：执行结果（与 steps 同序）
  editable: Boolean,
})
const emit = defineEmits(['select'])
const zoom = ref(null)

const detailOf = (i) => (props.detail || []).find(d => d.idx === i + 1)
const actionText = (s) => s.type === 'api'
  ? `${s.m} ${s.url}`
  : s.type === 'ai'
    ? `AI：${(s.goal || '').slice(0, 46)}`
    : ({ goto: '打开', click: '点击', fill: '输入', expect_text: '检查文字', screenshot: '截图' }[s.action] || s.action)
      + ' ' + (s.url || s.selector || s.value || '')
</script>

<template>
  <div class="lanes" v-if="roles.length">
    <div class="lane-heads">
      <div v-for="r in roles" :key="r.key" class="lane-head">{{ r.name }}</div>
    </div>
    <div class="lane-body">
      <div v-for="r in roles" :key="r.key" class="lane-col">
        <div v-for="i in steps.length" :key="i" class="cell">
          <template v-if="steps[i-1] && steps[i-1].role === r.key">
            <div class="stepcard" :class="{ fail: detailOf(i-1) && !detailOf(i-1).pass, done: detailOf(i-1) && detailOf(i-1).pass }"
                 @click="emit('select', i - 1)">
              <div class="sc-top">
                <span class="sc-no">{{ i }}</span>
                <span class="sc-type" :class="{ ai: steps[i-1].type === 'ai' }">{{ steps[i-1].type === 'ai' ? 'AI' : steps[i-1].type === 'ui' ? '页面' : '接口' }}</span>
                <span v-if="detailOf(i-1)" class="sc-st">{{ detailOf(i-1).pass ? '✓' : '✗' }}</span>
                <span v-if="steps[i-1].saved?.name || detailOf(i-1)?.saved" class="sc-share">共享</span>
              </div>
              <div class="sc-txt">{{ actionText(steps[i-1]) }}</div>
              <div v-if="detailOf(i-1) && !detailOf(i-1).pass" class="sc-err">{{ detailOf(i-1).reason }}</div>
              <img v-if="detailOf(i-1) && detailOf(i-1).screenshot" :src="detailOf(i-1).screenshot"
                   class="sc-shot" @click.stop="zoom = detailOf(i-1).screenshot">
            </div>
          </template>
        </div>
      </div>
    </div>
    <div v-if="!steps.length" class="empty" style="padding:24px">还没有步骤。可以点「录制」让浏览器帮你记，也可以手动添加。</div>

    <div v-if="zoom" class="shotbox" @click="zoom = null">
      <img :src="zoom">
      <div class="muted" style="color:#ccc;margin-top:10px">点击任意处关闭</div>
    </div>
  </div>
</template>

<style scoped>
.lanes{border:1px solid var(--line);border-radius:8px;background:#fff;overflow-x:auto}
.lane-heads{display:flex;position:sticky;top:0}
.lane-head{flex:1;min-width:170px;padding:9px 12px;font-size:13px;font-weight:600;color:#fff;background:#3b4252;border-right:1px solid rgba(255,255,255,.08)}
.lane-head:first-child{border-radius:8px 0 0 0}
.lane-head:last-child{border-radius:0 8px 0 0;border-right:none}
.lane-body{display:flex}
.lane-col{flex:1;min-width:170px;border-right:1px dashed var(--line)}
.lane-col:last-child{border-right:none}
.cell{min-height:64px;padding:8px;border-bottom:1px dashed var(--line2)}
.stepcard{border:1px solid var(--line);border-radius:7px;padding:8px 10px;background:#fff;cursor:pointer;font-size:12px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.stepcard:hover{border-color:var(--acc)}
.stepcard.done{border-color:#bfe3cc;background:#fbfefc}
.stepcard.fail{border-color:#f2c1c1;background:#fffafa}
.sc-top{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.sc-no{width:18px;height:18px;border-radius:50%;background:var(--ink);color:#fff;font-size:11px;display:flex;align-items:center;justify-content:center}
.sc-type{font-size:10.5px;color:var(--acc);background:var(--acc-weak);border-radius:3px;padding:0 5px}
.sc-type.ai{color:#7c3aed;background:#f3eefe}
.sc-st{margin-left:auto;font-weight:700}
.stepcard.done .sc-st{color:var(--ok)}
.stepcard.fail .sc-st{color:var(--err)}
.sc-share{font-size:10.5px;color:var(--warn);background:var(--warn-bg);border-radius:3px;padding:0 5px}
.sc-txt{font-family:var(--mono);font-size:11.5px;color:var(--txt);word-break:break-all}
.sc-err{color:var(--err);font-size:11px;margin-top:4px}
.sc-shot{max-width:100%;border:1px solid var(--line);border-radius:5px;margin-top:6px;cursor:zoom-in}
.shotbox{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:99;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:30px;cursor:zoom-out}
.shotbox img{max-width:94vw;max-height:82vh;border-radius:8px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
</style>
