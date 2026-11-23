import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { lstat } from 'node:fs/promises'
import { createConnection } from 'node:net'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'

export const name = 'agent-bridge-dsh-desktop-connector'
export const inject = ['sessionController']

const frameLimit = 16 * 1024 * 1024

async function connect(home, bridgeExecutable) {
  if (process.platform === 'win32') {
    const child = spawn(bridgeExecutable, ['_dsh-ipc', home], {
      stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true,
    })
    child.on('error', () => {})
    return { input: child.stdout, output: child.stdin, close: () => child.kill() }
  }
  const directory = join(home, 'dsh-ipc')
  const endpoint = join(directory, 'desktop.sock')
  const [root, dir, socket] = await Promise.all([lstat(home), lstat(directory), lstat(endpoint)])
  const uid = process.getuid()
  if (!root.isDirectory() || root.uid !== uid || (root.mode & 0o022) !== 0 ||
      !dir.isDirectory() || dir.uid !== uid || (dir.mode & 0o777) !== 0o700 ||
      !socket.isSocket() || socket.uid !== uid || (socket.mode & 0o777) !== 0o600) {
    throw new Error('Agent Bridge Desktop IPC requires a private, current-user socket')
  }
  const stream = createConnection({ path: endpoint })
  try {
    await once(stream, 'connect')
  } catch (error) {
    stream.destroy()
    throw error
  }
  return { input: stream, output: stream, close: () => stream.destroy() }
}

export async function serve(controller, transport, signal) {
  const current = new Map()
  const { input, output, close } = transport
  const onError = () => close()
  input.on('error', onError)
  output.on('error', onError)
  signal.addEventListener('abort', close, { once: true })
  const lines = createInterface({ input, crlfDelay: Infinity })
  lines.on('error', onError)
  async function send(frame) {
    const data = JSON.stringify(frame)
    if (Buffer.byteLength(data) >= frameLimit) throw new Error('IPC frame too large')
    await new Promise((done, fail) => output.write(data + '\n', error => error ? fail(error) : done()))
  }

  async function run(id, operation, args, active) {
    const value = async payload => {
      active.throwIfAborted()
      await send({ id, type: 'value', value: payload })
    }
    try {
      switch (operation) {
        case 'create': await value(await controller.create(args)); break
        case 'fork': await value(await controller.fork(args)); break
        case 'rename': await value(await controller.rename(args)); break
        case 'modelCatalog': await value(await controller.modelCatalog()); break
        case 'selectModel': await value(await controller.selectModel(args)); break
        case 'prompt': await value(await controller.prompt(args, active)); break
        case 'cancel': await value(await controller.cancel(args)); break
        case 'follow':
          for await (const frame of controller.follow(args, active)) {
            if (frame.type === 'snapshot') await value({ type: 'snapshot' })
            else if (frame.type === 'event') await value({ type: 'event', event: frame.event })
          }
          break
        default: throw new Error('Unsupported IPC operation')
      }
      if (!active.aborted) await send({ id, type: 'end' })
    } catch {
      if (!active.aborted) {
        try { await send({ id, type: 'error' }) } catch { close() }
      }
    } finally {
      current.delete(id)
    }
  }

  try {
    signal.throwIfAborted()
    await send({ dsh_ipc: 1 })
    for await (const line of lines) {
      if (Buffer.byteLength(line) >= frameLimit) break
      const { id, operation, args } = JSON.parse(line)
      if (typeof id !== 'string') break
      if (operation === 'close') {
        current.get(id)?.abort()
      } else if (!current.has(id)) {
        const abort = new AbortController()
        current.set(id, abort)
        void run(id, operation, args, abort.signal)
      }
    }
  } finally {
    signal.removeEventListener('abort', close)
    for (const abort of current.values()) abort.abort()
    lines.close()
    close()
  }
}

export function apply(ctx, config = {}) {
  const home = resolve(config.bridgeHome ?? process.env.AGENT_BRIDGE_HOME ?? join(homedir(), '.agent-bridge'))
  const executable = config.bridgeExecutable ?? 'agent-bridge'
  ctx.effect(() => {
    const stop = new AbortController()
    let retry
    async function run() {
      try {
        await serve(ctx.sessionController, await connect(home, executable), stop.signal)
      } catch {
        // Hub and Desktop may start in either order; the transport carries no credentials.
      }
      if (!stop.signal.aborted) retry = setTimeout(run, 1000)
    }
    void run()
    return () => { stop.abort(); clearTimeout(retry) }
  }, 'agent-bridge: same-user Desktop IPC')
}
