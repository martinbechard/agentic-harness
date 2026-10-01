import { spawn } from 'node:child_process';
import { Readable, Writable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import { ClientSideConnection, ndJsonStream, PROTOCOL_VERSION } from '@agentclientprotocol/sdk';

export async function bounded(promise, milliseconds=30_000) {
  let timer;
  try { return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('ACP request timed out')),milliseconds);})]); }
  finally { clearTimeout(timer); }
}

export function environment(source = process.env) {
  const env = Object.fromEntries(Object.entries(source).filter(([key]) => !key.endsWith('API_KEY') && ![
    'OPENAI_BASE_URL', 'OPENAI_API_BASE', 'DEFAULT_AUTH_REQUEST', 'MODEL_PROVIDER', 'CODEX_CONFIG', 'APP_SERVER_LOGS', 'CODEX_PATH'
  ].includes(key)));
  env.CODEX_CONFIG = JSON.stringify({ forced_login_method: 'chatgpt', model_provider: 'openai',
    'features.memories': false, 'sandbox_workspace_write.network_access': false });
  env.INITIAL_AGENT_MODE = 'workspace-write';
  return env;
}

export function appendMessage(text, lastMessage, sessionId, update) {
  const messageId=update.messageId ?? 'unidentified';
  if(lastMessage.get(sessionId)!==messageId) text.set(sessionId,'');
  lastMessage.set(sessionId,messageId);
  text.set(sessionId,(text.get(sessionId)??'')+update.content.text);
}

export class CodexClient {
  constructor(workspace) { this.workspace = workspace; this.events = []; this.responses = []; this.text = new Map(); this.lastMessage = new Map(); }
  async start() {
    const executable = fileURLToPath(new URL('./node_modules/@agentclientprotocol/codex-acp/dist/index.js', import.meta.url));
    this.child = spawn(process.execPath, [executable], { cwd: this.workspace, env: environment(), detached: true, stdio: ['pipe', 'pipe', 'ignore'] });
    this.exited = new Promise(resolve => this.child.once('exit', (code, signal) => resolve({code, signal})));
    this.connection = new ClientSideConnection(() => ({
      requestPermission: async params => {
        this.events.push({ sessionId: params.sessionId, event: 'permission_rejected' });
        return { outcome: { outcome: 'cancelled' } }; // permissions are not clarification answers
      },
      sessionUpdate: async ({sessionId, update}) => {
        if (update.sessionUpdate === 'agent_message_chunk' && update.content.type === 'text') {
          appendMessage(this.text, this.lastMessage, sessionId, update);
        }
        if (['tool_call', 'tool_call_update', 'usage_update', 'available_commands_update', 'current_mode_update', 'config_option_update', 'session_info_update'].includes(update.sessionUpdate)) {
          const {sessionUpdate, toolCallId, title, status, kind, used, size, cost, availableCommands, currentModeId} = update;
          this.events.push({sessionId, sessionUpdate, toolCallId, title, status, kind, used, size, cost,
            commands: availableCommands?.map(c => c.name), currentModeId, codex: update._meta?.codex ? {threadId:update._meta.codex.threadId,turnId:update._meta.codex.turnId}:undefined});
        }
      },
    }), ndJsonStream(Writable.toWeb(this.child.stdin), Readable.toWeb(this.child.stdout)));
    this.initialize = await bounded(this.connection.initialize({ protocolVersion: PROTOCOL_VERSION,
      clientInfo: {name:'northstar-acp-prototype', version:'0.1'}, clientCapabilities: {} }));
    // Maintained adapter extension delegates to app-server account/read; discard email.
    const auth = await bounded(this.connection.extMethod('authentication/status', {}));
    this.auth = {type: auth.type};
    if (auth.type !== 'chat-gpt') throw new Error('ChatGPT subscription login required; no API-key/gateway fallback');
  }
  async newSession(mode = 'workspace-write') {
    const result = await bounded(this.connection.newSession({cwd: this.workspace, mcpServers: []}));
    await bounded(this.connection.setSessionMode({sessionId: result.sessionId, modeId: mode}));
    if (process.env.PROTOTYPE_MODEL) {
      await bounded(this.connection.setSessionConfigOption({ sessionId: result.sessionId, configId: 'model', value: process.env.PROTOTYPE_MODEL }));
    }
    return result;
  }
  async loadSession(sessionId, mode='read-only') {
    const result=await bounded(this.connection.loadSession({sessionId,cwd:this.workspace,mcpServers:[]}));
    await bounded(this.connection.setSessionMode({sessionId,modeId:mode}));
    return result;
  }
  async prompt(sessionId, text) {
    this.text.set(sessionId, ''); this.lastMessage.delete(sessionId);
    const response = await bounded(this.connection.prompt({sessionId, prompt: [{type:'text', text}]}),300_000);
    this.responses.push({sessionId, ...response});
    if (response.stopReason !== 'end_turn') throw new Error(`Unsuccessful native turn: ${response.stopReason}`);
    return this.text.get(sessionId) ?? '';
  }
  close() {
    if (!this.closing) this.closing=this._close();
    return this.closing;
  }
  async _close() {
    if (!this.child) return;
    // Kill the private process group, including adapter's app-server child.
    try { process.kill(-this.child.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    const escalation=setTimeout(()=>{try{process.kill(-this.child.pid,'SIGKILL');}catch{}},3000);
    try {this.cleanup = await bounded(this.exited,6000);} finally {clearTimeout(escalation);}
    for (let i=0; i<30; i++) {
      try { process.kill(-this.child.pid, 0); } catch (error) {
        if (error.code === 'ESRCH') { this.cleanup.processGroupGone=true; return; }
        throw error;
      }
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    try { process.kill(-this.child.pid, 'SIGKILL'); } catch (error) { if(error.code!=='ESRCH') throw error; }
    await new Promise(resolve=>setTimeout(resolve,100));
    try { process.kill(-this.child.pid,0); this.cleanup.processGroupGone=false; }
    catch(error) { if(error.code!=='ESRCH') throw error; this.cleanup.processGroupGone=true; }

  }
}
