/**
 * 设置页 · MCP 服务器管理（从 App.vue 抽出，2026-10-09 设置页拆分）。
 *
 * 覆盖列表加载、增删改对话框、「测试 / 启动（含后台轮询）」与状态徽标计算。
 * App.vue 创建唯一实例 provide('settingsMcp')，工具面板经 inject 读写；
 * 打开设置页时由 App.vue 的 watch(settingsVisible) 调 loadMcp() 刷新。
 */
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  connectMcpServers,
  listMcpServers,
  saveMcpServers,
  testMcpServer,
} from '../api'

export function useMcpServers() {
  const mcpServers = ref([])
  const mcpDialogVisible = ref(false)
  const mcpTesting = ref(false)
  const mcpStatus = ref({}) // GET /mcp 返回的运行时状态（连接/工具/错误）
  const mcpTested = ref({}) // 本地「测试」结果（服务器未启动时也给反馈）
  const mcpExpanded = ref(new Set()) // 展开工具列表的服务器 id
  const mcpEditingId = ref('')
  const mcpConnecting = ref(false)
  const mcpForm = ref({
    name: '',
    type: 'stdio',
    command: '',
    url: '',
    envText: '',
    headersText: '',
  })

  async function loadMcp() {
    try {
      const data = await listMcpServers()
      mcpServers.value = data.servers || []
      mcpStatus.value = data.status || {}
    } catch {
      mcpServers.value = []
      mcpStatus.value = {}
    }
  }

  function openMcpDialog(server) {
    if (server) {
      mcpEditingId.value = server.id
      mcpForm.value = {
        name: server.name || '',
        type: server.type || 'stdio',
        command: server.type === 'http' ? '' : [server.command, ...(server.args || [])].join(' '),
        url: server.url || '',
        envText: Object.entries(server.env || {})
          .map(([k, v]) => `${k}=${v}`)
          .join('\n'),
        headersText: Object.entries(server.headers || {})
          .map(([k, v]) => `${k}=${v}`)
          .join('\n'),
      }
    } else {
      mcpEditingId.value = ''
      mcpForm.value = {
        name: '',
        type: 'stdio',
        command: '',
        url: '',
        envText: '',
        headersText: '',
      }
    }
    mcpDialogVisible.value = true
  }

  /** 解析「每行 KEY=VALUE」文本域为对象。 */
  function parseKvLines(text) {
    const out = {}
    for (const line of String(text || '').split('\n')) {
      const trimmed = line.trim()
      if (!trimmed) continue
      const idx = trimmed.indexOf('=')
      if (idx <= 0) continue
      const key = trimmed.slice(0, idx).trim()
      const value = trimmed.slice(idx + 1).trim()
      if (key) out[key] = value
    }
    return out
  }

  /** 状态徽标：运行时状态优先，其次最近一次测试结果。 */
  function mcpBadge(s) {
    if (!s.enabled) {
      return { tone: 'idle', text: '已停用', title: '该服务器已关闭，打开右侧开关后可用' }
    }
    const st = mcpStatus.value[s.id]
    if (st) {
      if (st.connected) {
        return { tone: 'ok', text: `已连接 · ${st.tool_count} 工具`, title: '运行时连接正常' }
      }
      if (st.connecting) return { tone: 'idle', text: '连接中…', title: '后台正在连接' }
      if (st.cached) {
        return { tone: 'warn', text: `离线 · 缓存 ${st.tool_count} 工具`, title: st.error || '连接失败，使用上次缓存' }
      }
      if (st.error) return { tone: 'bad', text: '连接失败', title: st.error }
      return { tone: 'idle', text: '未连接', title: st.error || '等待连接' }
    }
    const tr = mcpTested.value[s.id]
    if (tr) {
      return tr.ok
        ? {
            tone: 'ok',
            text: `测试通过 · ${tr.tools.length} 工具`,
            title: `${tr.at} 连接正常（${tr.latency_ms}ms）`,
          }
        : { tone: 'bad', text: '测试失败', title: tr.error || '' }
    }
    return { tone: 'idle', text: '未启动', title: '尚未连接：点「测试」或发一条消息后显示' }
  }

  function mcpTools(s) {
    const st = mcpStatus.value[s.id]
    if (st && st.tools && st.tools.length) return st.tools
    const tr = mcpTested.value[s.id]
    return (tr && tr.tools) || []
  }

  function toggleMcpTools(id) {
    const next = new Set(mcpExpanded.value)
    next.has(id) ? next.delete(id) : next.add(id)
    mcpExpanded.value = next
  }

  function mcpEnvSummary(s) {
    const parts = []
    const envKeys = Object.keys(s.env || {})
    const headerKeys = Object.keys(s.headers || {})
    if (envKeys.length) parts.push(`环境变量 ${envKeys.length} 项`)
    if (headerKeys.length) parts.push(`请求头 ${headerKeys.length} 项`)
    return parts.join(' · ')
  }

  async function saveMcpDialog() {
    const f = mcpForm.value
    if (!f.name.trim() || (f.type === 'stdio' ? !f.command.trim() : !f.url.trim())) {
      ElMessage.warning('名称和启动命令/URL 不能为空')
      return
    }
    mcpTesting.value = true
    try {
      const env = parseKvLines(f.envText)
      const headers = parseKvLines(f.headersText)
      if (mcpEditingId.value) {
        const target = mcpServers.value.find((x) => x.id === mcpEditingId.value)
        if (target) {
          target.name = f.name.trim()
          if (f.type === 'stdio') {
            const parts = f.command.trim().split(/\s+/)
            target.command = parts[0]
            target.args = parts.slice(1)
            target.env = env
          } else {
            target.url = f.url.trim()
            target.headers = headers
          }
        }
        await saveMcpList()
        mcpDialogVisible.value = false
        ElMessage.success('已保存，命令/凭据变更会触发重连')
      } else {
        const cfg = {
          id: `mcp_${Date.now()}`,
          name: f.name.trim(),
          type: f.type,
          command: '',
          args: [],
          url: '',
          enabled: true,
          env: {},
          headers: {},
        }
        if (f.type === 'stdio') {
          const parts = f.command.trim().split(/\s+/)
          cfg.command = parts[0]
          cfg.args = parts.slice(1)
          cfg.env = env
        } else {
          cfg.url = f.url.trim()
          cfg.headers = headers
        }
        mcpServers.value.push(cfg)
        await saveMcpList()
        mcpDialogVisible.value = false
        ElMessage.success('MCP 服务器已添加')
      }
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存失败')
    } finally {
      mcpTesting.value = false
    }
  }

  async function saveMcpList() {
    try {
      const data = await saveMcpServers(mcpServers.value)
      if (data && data.status) mcpStatus.value = data.status
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '保存 MCP 配置失败')
    }
  }

  async function testMcp(s) {
    try {
      const result = await testMcpServer(s)
      mcpTested.value = {
        ...mcpTested.value,
        [s.id]: {
          ok: !!result.ok,
          tools: result.tools || [],
          latency_ms: result.latency_ms,
          error: result.error,
          at: new Date().toLocaleTimeString(),
        },
      }
      if (result.ok) {
        const list = (result.tools || []).join(', ') || '（无）'
        ElMessageBox.alert(
          `连接成功（${result.latency_ms}ms）\n工具（${(result.tools || []).length}）：${list}`,
          'MCP 测试',
          { confirmButtonText: '知道了' },
        )
      } else {
        ElMessageBox.alert(result.error || '连接失败', 'MCP 测试', { confirmButtonText: '知道了' })
      }
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '测试失败')
    }
  }

  /** 「启动」：让运行时管理器立即连接（后台），轮询状态直到出结果。 */
  async function connectMcp(s) {
    const targets = (s ? [s] : mcpServers.value.filter((x) => x.enabled)).filter(
      (x) => mcpBadge(x).tone !== 'ok',
    )
    const ids = targets.map((x) => x.id)
    if (!ids.length) {
      ElMessage.info('没有需要启动的服务器')
      return
    }
    mcpConnecting.value = true
    try {
      const res = await connectMcpServers(ids)
      if (res.status) mcpStatus.value = res.status
      if (!res.ok) {
        ElMessage.error(res.error || '启动失败')
        return
      }
      // 后台轮询：直到全部出结果（连接成功/失败）或超时（约 100s）
      for (let i = 0; i < 40; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2500))
        const data = await listMcpServers()
        mcpStatus.value = data.status || {}
        const settled = ids.every((id) => {
          const st = mcpStatus.value[id]
          return st && (st.connected || st.error)
        })
        if (settled) break
      }
      const okCount = ids.filter((id) => mcpStatus.value[id]?.connected).length
      if (okCount === ids.length) ElMessage.success(`已启动 ${okCount} 个服务器`)
      else ElMessage.warning(`${okCount}/${ids.length} 个已连接，详情看状态徽标`)
    } catch (error) {
      const detail = error.response?.data?.detail
      ElMessage.error(
        error.response?.status === 404 ? '后端未加载新接口，请重启后端' : detail || '启动失败',
      )
    } finally {
      mcpConnecting.value = false
    }
  }

  async function removeMcp(s) {
    try {
      await ElMessageBox.confirm(`确定删除 MCP 服务器「${s.name}」吗？`, '提示', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
    mcpServers.value = mcpServers.value.filter((x) => x.id !== s.id)
    await saveMcpList()
  }

  return {
    mcpServers,
    mcpDialogVisible,
    mcpTesting,
    mcpStatus,
    mcpTested,
    mcpExpanded,
    mcpEditingId,
    mcpConnecting,
    mcpForm,
    loadMcp,
    openMcpDialog,
    mcpBadge,
    mcpTools,
    toggleMcpTools,
    mcpEnvSummary,
    saveMcpDialog,
    saveMcpList,
    testMcp,
    connectMcp,
    removeMcp,
  }
}
