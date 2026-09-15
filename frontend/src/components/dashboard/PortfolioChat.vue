<template>
  <div class="portfolio-chat">
    <div class="agent-card">
      <div class="agent-card-header">
        <div class="agent-card-avatar">P</div>
        <div class="agent-card-info">
          <div class="agent-card-name">PortfolioAgent</div>
          <div class="agent-card-subtitle">
            Answers grounded only in this portfolio's own numbers — never invents figures.
          </div>
        </div>
      </div>
      <div class="agent-card-body">
        <div class="tool-item">
          <div class="tool-content">
            <div class="tool-name">Full multi-round debate</div>
            <div class="tool-desc">
              Run a fresh 5-investor + Gamma debate for
              <strong>{{ ticker || 'the selected ticker' }}</strong> instead of a quick answer.
            </div>
          </div>
          <button class="btn-secondary" :disabled="!ticker" @click="$emit('run-debate', ticker)">
            Run Debate
          </button>
        </div>
      </div>
    </div>

    <div class="chat-messages" ref="chatMessagesEl">
      <div v-if="chatHistory.length === 0" class="chat-empty">
        <p>Ask a question about this portfolio, e.g. "Why is MSFT at 20% weight?"</p>
      </div>
      <div v-for="(msg, idx) in chatHistory" :key="idx" class="chat-message" :class="msg.role">
        <div class="message-avatar">{{ msg.role === 'user' ? 'U' : 'P' }}</div>
        <div class="message-content">
          <div class="message-header">
            <span class="sender-name">{{ msg.role === 'user' ? 'You' : 'PortfolioAgent' }}</span>
            <span class="message-time">{{ formatTime(msg.timestamp) }}</span>
          </div>
          <div class="message-text">{{ msg.content }}</div>
        </div>
      </div>
      <div v-if="isSending" class="chat-message assistant">
        <div class="message-avatar">P</div>
        <div class="message-content">
          <div class="typing-indicator"><span></span><span></span><span></span></div>
        </div>
      </div>
    </div>

    <div v-if="askError" class="error-banner">{{ askError }}</div>

    <div class="chat-input-area">
      <textarea
        v-model="question"
        class="chat-input"
        placeholder="Ask about this portfolio..."
        rows="1"
        :disabled="isSending"
        @keydown.enter.exact.prevent="sendMessage"
      ></textarea>
      <button class="send-btn" :disabled="!question.trim() || isSending" @click="sendMessage">→</button>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { askPortfolio } from '../../api/portfolio'

const props = defineProps({
  portfolio: { type: Object, default: null },
  ticker: { type: String, default: '' }
})
const emit = defineEmits(['add-log', 'run-debate'])

const chatHistory = ref([])
const question = ref('')
const isSending = ref(false)
const askError = ref('')
const chatMessagesEl = ref(null)

function formatTime(ts) {
  try {
    return new Date(ts).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (chatMessagesEl.value) chatMessagesEl.value.scrollTop = chatMessagesEl.value.scrollHeight
  })
}

async function sendMessage() {
  const q = question.value.trim()
  if (!q || isSending.value) return
  question.value = ''
  askError.value = ''
  chatHistory.value.push({ role: 'user', content: q, timestamp: new Date().toISOString() })
  scrollToBottom()
  isSending.value = true
  emit('add-log', `Asked PortfolioAgent: "${q.slice(0, 60)}"`)
  try {
    const result = await askPortfolio(q, props.portfolio)
    chatHistory.value.push({ role: 'assistant', content: result.answer, timestamp: new Date().toISOString() })
    emit('add-log', 'PortfolioAgent answered.')
  } catch (err) {
    askError.value = err.message || 'Unknown error while answering the question.'
    emit('add-log', `PortfolioAgent error: ${askError.value}`)
  } finally {
    isSending.value = false
    scrollToBottom()
  }
}
</script>

<style scoped>
.portfolio-chat {
  height: 100%;
  display: flex;
  flex-direction: column;
  font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
}

.agent-card {
  border-bottom: 1px solid #EAEAEA;
  background: linear-gradient(135deg, #F8FAFC 0%, #F1F5F9 100%);
  flex-shrink: 0;
}

.agent-card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
}

.agent-card-avatar {
  width: 38px;
  height: 38px;
  min-width: 38px;
  background: linear-gradient(135deg, #1F2937 0%, #374151 100%);
  color: #FFF;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 700;
}

.agent-card-info {
  min-width: 0;
}

.agent-card-name {
  font-size: 13px;
  font-weight: 700;
  color: #1F2937;
}

.agent-card-subtitle {
  font-size: 11px;
  color: #6B7280;
}

.agent-card-body {
  padding: 0 18px 14px;
}

.tool-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  background: #FFF;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
}

.tool-name {
  font-size: 11px;
  font-weight: 700;
  color: #1F2937;
  margin-bottom: 2px;
}

.tool-desc {
  font-size: 10px;
  color: #6B7280;
}

.btn-secondary {
  background: #FFF;
  border: 1px solid #1F2937;
  color: #1F2937;
  padding: 6px 12px;
  font-family: inherit;
  font-size: 11px;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
}

.btn-secondary:disabled {
  color: #999;
  border-color: #CCC;
  cursor: not-allowed;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.chat-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #9CA3AF;
  font-size: 12px;
  text-align: center;
}

.chat-message {
  display: flex;
  gap: 10px;
}

.chat-message.user {
  flex-direction: row-reverse;
}

.message-avatar {
  width: 28px;
  height: 28px;
  min-width: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
}

.chat-message.user .message-avatar {
  background: #1F2937;
  color: #FFF;
}

.chat-message.assistant .message-avatar {
  background: #F3F4F6;
  color: #374151;
}

.message-content {
  max-width: 75%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.chat-message.user .message-content {
  align-items: flex-end;
}

.message-header {
  display: flex;
  gap: 6px;
  align-items: center;
}

.chat-message.user .message-header {
  flex-direction: row-reverse;
}

.sender-name {
  font-size: 11px;
  font-weight: 700;
  color: #374151;
}

.message-time {
  font-size: 10px;
  color: #9CA3AF;
}

.message-text {
  padding: 8px 12px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.chat-message.user .message-text {
  background: #1F2937;
  color: #FFF;
  border-bottom-right-radius: 3px;
}

.chat-message.assistant .message-text {
  background: #F3F4F6;
  color: #374151;
  border-bottom-left-radius: 3px;
}

.typing-indicator {
  display: flex;
  gap: 4px;
  padding: 8px 12px;
  background: #F3F4F6;
  border-radius: 10px;
}

.typing-indicator span {
  width: 6px;
  height: 6px;
  background: #9CA3AF;
  border-radius: 50%;
  animation: typing 1.4s infinite ease-in-out;
}

.typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
.typing-indicator span:nth-child(3) { animation-delay: 0.4s; }

@keyframes typing {
  0%, 60%, 100% { transform: translateY(0); }
  30% { transform: translateY(-6px); }
}

.error-banner {
  margin: 0 18px 10px;
  background: #FFF0EF;
  border: 1px solid #D32F2F;
  color: #D32F2F;
  padding: 8px 12px;
  font-size: 11px;
  border-radius: 3px;
}

.chat-input-area {
  padding: 12px 18px;
  border-top: 1px solid #E5E7EB;
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-shrink: 0;
}

.chat-input {
  flex: 1;
  padding: 9px 12px;
  font-size: 12px;
  font-family: inherit;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  resize: none;
}

.chat-input:focus {
  outline: none;
  border-color: #1F2937;
}

.send-btn {
  width: 36px;
  height: 36px;
  background: #1F2937;
  color: #FFF;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 16px;
}

.send-btn:disabled {
  background: #E5E7EB;
  color: #9CA3AF;
  cursor: not-allowed;
}
</style>
