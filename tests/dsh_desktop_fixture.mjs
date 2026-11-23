import { EventEmitter, on } from 'node:events'

import { apply } from '../dsh-desktop-connector/index.js'

const histories = new Map()
const streams = new Map()
let nextId = 0
function create(history) {
  const sessionId = String(++nextId)
  histories.set(sessionId, [...history])
  streams.set(sessionId, new EventEmitter())
  return { sessionId }
}

const controller = {
  create: () => create([]),
  fork: ({ sessionId }) => create(histories.get(sessionId)),
  rename: () => ({}),
  async *follow({ address }, signal) {
    const events = on(streams.get(address.sessionId), 'frame', { signal })
    try {
      yield { type: 'snapshot', records: ['r'.repeat(17 * 1024 * 1024)] }
      for await (const [event] of events) yield { type: 'event', event }
    } finally {
      await events.return()
    }
  },
  prompt({ sessionId, requestId, content }) {
    const history = histories.get(sessionId)
    const stream = streams.get(sessionId)
    history.push(content[0].text)
    const turn = history.length
    stream.emit('frame', { type: 'turn/start', data: { turn } })
    stream.emit('frame', { type: 'user/message', data: { source: { rpcId: requestId } } })
    stream.emit('frame', { type: 'tool/call', data: { callId: 'example', name: 'list', arguments: '{}' } })
    stream.emit('frame', { type: 'tool/result', data: { message: { toolCallId: 'example', isError: false } } })
    stream.emit('frame', { type: 'tool/result', surfaceOp: { op: 'replace', startSeq: 12, endSeq: 12 },
      data: { message: { toolCallId: 'example', isError: false } } })
    stream.emit('frame', { type: 'tool/result', surfaceOp: { op: 'replace', startSeq: 4, endSeq: 4 },
      data: { message: { toolCallId: 'earlier-turn', isError: false } } })
    stream.emit('frame', { type: 'tool/result', data: { message: { toolCallId: 'example', isError: false } } })
    stream.emit('frame', { type: 'tool/call', data: { callId: 'other', name: 'read', arguments: '{}' } })
    stream.emit('frame', { type: 'tool/result', data: { message: { toolCallId: 'other', isError: true } } })
    stream.emit('frame', { type: 'assistant/message', data: {
      message: { content: [{ type: 'text', text: history.join(' ') }] },
    } })
    stream.emit('frame', { type: 'turn/end', data: { turn, reason: { kind: 'completed' } } })
    return { accepted: true }
  },
}

let dispose
apply({ sessionController: controller, effect(fn) { dispose = fn() } }, {
  bridgeHome: process.argv[2],
  bridgeExecutable: process.argv[3],
})
process.on('SIGTERM', () => dispose())
