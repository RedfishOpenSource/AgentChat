// WebSocket 联调测试：经 vite 代理连接后端 /ws，注入消息后应收到 message/reply 事件
const ws = new WebSocket('ws://127.0.0.1:5173/ws')
const seen = []
let done = false

ws.onopen = async () => {
  console.log('WS CONNECTED')
  await fetch('http://127.0.0.1:5173/api/simulate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform: 'dingtalk', chat_id: 'ws-test', sender: '王五', content: '辛苦了，谢谢！' }),
  })
}

ws.onmessage = (e) => {
  const ev = JSON.parse(e.data)
  seen.push(ev.type)
  if (ev.type === 'reply') {
    console.log('REPLY:', ev.message.content, '| status:', ev.message.status)
  }
  if (seen.length >= 3) {
    console.log('EVENT TYPES:', seen.join(' -> '))
    const ok = seen[0] === 'hello' && seen.includes('message') && seen.includes('reply')
    console.log(ok ? 'WS-TEST-PASS' : 'WS-TEST-FAIL')
    ws.close()
    done = true
    process.exit(ok ? 0 : 1)
  }
}

ws.onerror = (e) => { console.log('WS ERROR'); process.exit(1) }
setTimeout(() => { console.log('WS TIMEOUT', seen.join(',')); process.exit(1) }, 10000)
