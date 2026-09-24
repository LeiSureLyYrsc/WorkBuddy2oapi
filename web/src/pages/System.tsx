// System.tsx 系统页：运行信息、容器状态与控制、任务历史、使用说明。
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api'
import type { SessionInfo, SystemInfo, TaskListResponse } from '../types'
import { Alert, Badge, fmtDuration, fmtISO, Modal, Spinner } from '../ui'

export default function System({
  session,
  onSessionRefresh,
}: {
  session: SessionInfo
  onSessionRefresh: () => Promise<SessionInfo | null>
}) {
  const [info, setInfo] = useState<SystemInfo | null>(null)
  const [tasks, setTasks] = useState<TaskListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const [i, t] = await Promise.all([api.system(), api.tasks()])
      setInfo(i)
      setTasks(t)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载系统信息失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(true), 20_000)
    return () => clearInterval(timer)
  }, [load])

  if (loading && !info) return <Spinner label="正在加载系统信息…" />

  const gatewayServiceOK = info?.gateway_health?.service === 'workbuddy2api'

  return (
    <>
      <div className="page-head">
        <div>
          <h1>系统</h1>
          <p>面板与网关的运行状态与任务历史</p>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={() => void load()} disabled={loading}>
            {loading ? <Spinner /> : '🔄'} 刷新
          </button>
        </div>
      </div>

      {notice && (
        <Alert kind="ok" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}
      {error && (
        <Alert kind="error" onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {session.using_default_password && (
        <Alert kind="warn">
          面板正在使用默认口令，请在服务端配置中修改 <span className="mono">ui.password</span>。
        </Alert>
      )}

      {/* 网关与容器状态 */}
      <div className="card">
        <div className="card-head">
          <h2>网关状态</h2>
          {gatewayServiceOK ? <Badge cls="badge-ok">运行中</Badge> : <Badge cls="badge-danger">不可达</Badge>}
        </div>
        <dl className="kv">
          <dt>网关地址</dt>
          <dd className="mono">{info?.gateway_url}</dd>
          <dt>身份标识</dt>
          <dd>
            {gatewayServiceOK ? (
              <>service = <span className="mono">workbuddy2api</span> ✅</>
            ) : (
              <span className="text-danger">{info?.gateway_health_error || '无法确认身份'}</span>
            )}
          </dd>
          <dt>账号池</dt>
          <dd>
            {info?.gateway_health ? `${info.gateway_health.healthy} / ${info.gateway_health.total} 可用` : '—'}
          </dd>
        </dl>
        {!session.dangerous_ops && (
          <div className="desc" style={{ marginTop: 12 }}>
            当前未开启高危操作（<span className="mono">dangerous_ops=false</span>），因此「删除账号」
            「恢复配置备份」均被禁用。如需启用，在面板服务端配置中设置：
            <div className="muted-box" style={{ marginTop: 7 }}>{`WBGUI_DANGEROUS_OPS=true  # 或配置文件里 "dangerous_ops": true`}</div>
          </div>
        )}
      </div>

      {/* 面板信息 */}
      <div className="card">
        <div className="card-head">
          <h2>面板信息</h2>
          <button
            className="btn btn-sm"
            onClick={() => setShowPassword(true)}
            disabled={!session.password_changeable}
            title={session.password_changeable ? '修改面板登录口令' : '服务端未配置凭据持久化，无法从网页改密码'}
          >
            🔑 修改密码
          </button>
        </div>
        <dl className="kv">
          <dt>面板版本</dt>
          <dd className="mono">{info?.version || 'dev'}</dd>
          <dt>运行时长</dt>
          <dd>{info ? fmtDuration(info.uptime_sec) : '—'}</dd>
          <dt>启动时间</dt>
          <dd>{info ? fmtISO(info.started_at) : '—'}</dd>
          <dt>凭证目录</dt>
          <dd className="mono">{info?.auth_dir}</dd>
          <dt>网关配置文件</dt>
          <dd className="mono">{info?.config_file}</dd>
          <dt>运行模式</dt>
          <dd>
            {info?.read_only ? <Badge cls="badge-warn">只读</Badge> : <Badge cls="badge-ok">可写</Badge>}
            {info?.dangerous_ops ? (
              <Badge cls="badge-warn">高危操作已解锁</Badge>
            ) : (
              <Badge cls="badge-dim">高危操作已锁定</Badge>
            )}
          </dd>
          <dt>热重载</dt>
          <dd>{info?.hot_reload !== false ? '✅ 账号与配置均支持热加载（无需重启）' : '❌ 未启用'}</dd>
        </dl>
      </div>

      {/* 任务历史 */}
      <div className="card">
        <div className="card-head">
          <h2>任务历史</h2>
          <span className="hint">保留最近 20 条（进程重启后清空）</span>
        </div>
        {tasks?.running && tasks.running.length > 0 && (
          <Alert kind="info">
            当前有 {tasks.running.length} 个任务正在执行：{tasks.running.map((t) => t.title).join('、')}
          </Alert>
        )}
        {!tasks?.tasks || tasks.tasks.length === 0 ? (
          <div className="empty">
            还没有执行过批量任务。
            <div style={{ marginTop: 10 }}>
              <Link className="btn btn-sm" to="/accounts">
                去账号管理执行
              </Link>
            </div>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>状态</th>
                  <th className="num">成功 / 失败</th>
                  <th>开始时间</th>
                </tr>
              </thead>
              <tbody>
                {tasks.tasks.map((t) => (
                  <tr key={t.id}>
                    <td>{t.title}</td>
                    <td>
                      {t.running ? (
                        <Badge cls="badge-accent">执行中</Badge>
                      ) : t.failed > 0 ? (
                        <Badge cls="badge-warn">已完成（有失败）</Badge>
                      ) : (
                        <Badge cls="badge-ok">已完成</Badge>
                      )}
                    </td>
                    <td className="num">
                      <span className="text-ok">{t.ok}</span> /{' '}
                      {t.failed > 0 ? <span className="text-danger">{t.failed}</span> : <span className="text-dim">0</span>}
                    </td>
                    <td className="text-dim" style={{ fontSize: 12 }}>
                      {fmtISO(t.started_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 使用说明 */}
      <div className="card">
        <div className="card-head">
          <h2>客户端接入方式</h2>
        </div>
        <p className="text-dim" style={{ marginTop: 0, fontSize: 13 }}>
          网关对客户端暴露标准 OpenAI 兼容接口，任何支持自定义 base_url 的客户端都可直接接入：
        </p>
        <div className="muted-box">
          {`Base URL:  ${info?.gateway_url || 'http://127.0.0.1:7863'}/v1
API Key:   你在网关 config.json 中设置的 api_key
模型:      见「聊天测试」页的模型列表（如 deepseek-v4.1-flash、glm-5.3 等）`}
        </div>
        <div className="muted-box" style={{ marginTop: 10 }}>
          {`# 命令行验证
curl ${info?.gateway_url || 'http://127.0.0.1:7863'}/v1/chat/completions \\
  -H "Authorization: Bearer <你的 api_key>" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}],"stream":true}'`}
        </div>
        <div className="desc" style={{ marginTop: 10 }}>
          提示：单进程单端口同时提供 OpenAI 兼容 API 与控制台；账号与配置热加载，无需重启。
        </div>
      </div>

      {showPassword && (
        <ChangePasswordDialog
          currentUser={session.username}
          onClose={() => setShowPassword(false)}
          onDone={async () => {
            setShowPassword(false)
            await onSessionRefresh()
          }}
        />
      )}
    </>
  )
}

/** ChangePasswordDialog 修改面板登录口令。 */
function ChangePasswordDialog({
  currentUser,
  onClose,
  onDone,
}: {
  currentUser: string
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [newUsername, setNewUsername] = useState(currentUser)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const submit = async () => {
    setError(null)
    if (!current) {
      setError('请输入当前口令')
      return
    }
    if (next.length < 6) {
      setError('新口令至少 6 位')
      return
    }
    if (next !== confirm) {
      setError('两次输入的新口令不一致')
      return
    }
    setBusy(true)
    try {
      const res = await api.changePassword(current, next, newUsername)
      setDone(res.message || '口令已修改')
      // 改成功后可无缝继续（后端已重发会话 Cookie）。
      await onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '修改失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="修改登录口令"
      onClose={onClose}
      footer={
        done ? (
          <button className="btn btn-primary" onClick={onClose}>
            完成
          </button>
        ) : (
          <>
            <button className="btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button className="btn btn-primary" onClick={() => void submit()} disabled={busy}>
              {busy ? <Spinner /> : null}
              保存修改
            </button>
          </>
        )
      }
    >
      {done ? (
        <Alert kind="ok">{done}</Alert>
      ) : (
        <>
          {error && <Alert kind="error">{error}</Alert>}
          <div className="field">
            <label>用户名</label>
            <input type="text" value={newUsername} onChange={(e) => setNewUsername(e.target.value)} autoComplete="username" />
            <div className="desc">可同时修改登录用户名，留空则沿用当前用户名。</div>
          </div>
          <div className="field">
            <label>当前口令</label>
            <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" autoFocus />
          </div>
          <div className="field">
            <label>新口令（至少 6 位）</label>
            <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>确认新口令</label>
            <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="desc" style={{ marginTop: 12 }}>
            修改后所有已登录会话立即失效，需用新口令重新登录（本页面会自动续期）。
          </div>
        </>
      )}
    </Modal>
  )
}
