import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { postAction, request } from '@/api/client'

const ENDPOINT = '/api/snapshot'

const columns = [
  '快照编号',
  '表名称',
  '快照时间',
  '快照大小',
  '保留天数',
  '恢复校验',
  '快照状态',
  '存储校验',
  '引用批次',
  '失败证据',
]

/** 每个状态允许的动作直接对齐后端状态机，前端不再让所有动作永远可点。 */
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  草稿: ['开始校验', '重试存储校验'],
  运行中: ['校验通过', '校验未通过'],
  已完成: ['清理快照'],
  已清理: ['归档记录'],
}

const CREATE_FIELDS = [
  { key: '快照编号', required: true, placeholder: '例如 SNAP-0004' },
  { key: '表名称', required: true, placeholder: '例如 dws_order_summary' },
  { key: '快照时间', required: true, placeholder: 'YYYY-MM-DD HH:mm:ss' },
  { key: '快照大小', required: false, placeholder: '例如 8.7GB' },
  { key: '保留天数', required: false, placeholder: '例如 30' },
  { key: '创建批次', required: false, placeholder: '例如 BATCH-20260902-01' },
]

type Row = Record<string, string | number | boolean | null>
type EvidenceRow = Record<string, string | boolean | null>

function Badge({ tone, text }: { tone: 'ok' | 'no' | 'info' | 'warn'; text: string }) {
  return <span className={`badge badge-${tone}`}>{text}</span>
}

function statusTone(status: string): 'ok' | 'no' | 'info' | 'warn' {
  if (status === '已完成') return 'ok'
  if (status === '运行中') return 'info'
  if (status === '已清理') return 'warn'
  return 'no'
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="modal-close" type="button" onClick={onClose} aria-label="关闭">×</button>
        </div>
        {children}
      </div>
    </div>
  )
}

export default function SnapshotView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [evidence, setEvidence] = useState<EvidenceRow[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [showEvidence, setShowEvidence] = useState(false)
  const [createForm, setCreateForm] = useState<Record<string, string>>({})
  const [dialog, setDialog] = useState<{ action: string; row: Row } | null>(null)
  const [actionNote, setActionNote] = useState('')
  const [simulateStorageFail, setSimulateStorageFail] = useState(false)
  const filterFields = useMemo(() => ['快照编号', '快照状态'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    // 后端列表接口按 keyword/status 过滤，中文筛选字段在这里统一映射，避免页面各传各的。
    const params = new URLSearchParams()
    if (filters['快照编号']) params.set('keyword', filters['快照编号'])
    if (filters['快照状态']) params.set('status', filters['快照状态'])
    const query = params.toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('快照记录列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照管理列表读取失败')
    }
  }, [filters])

  const loadEvidence = useCallback(async () => {
    const response = await request(`${ENDPOINT}/recovery`)
    if (response.ok) {
      const payload = await response.json()
      setEvidence(payload.items ?? [])
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void loadEvidence()
  }, [loadEvidence])

  async function runAction(action: string, row: Row, extra: Record<string, unknown> = {}) {
    setErrorMessage('')
    setNotice('')
    const outcome = await postAction(`${ENDPOINT}/${row.id}/actions`, { action, ...extra })
    if (!outcome.ok) {
      setErrorMessage(outcome.message)
    } else {
      setNotice(outcome.message)
    }
    setDialog(null)
    setActionNote('')
    await reload()
    await loadEvidence()
  }

  async function submitCreate() {
    setErrorMessage('')
    setNotice('')
    const values: Record<string, unknown> = { ...createForm }
    if (simulateStorageFail) {
      values['模拟存储校验结果'] = '失败'
    }
    const outcome = await postAction(ENDPOINT, values)
    if (!outcome.ok) {
      setErrorMessage(outcome.message)
      return
    }
    setNotice(outcome.message)
    setShowCreate(false)
    setCreateForm({})
    setSimulateStorageFail(false)
    await reload()
  }

  function openActionDialog(action: string, row: Row) {
    setDialog({ action, row })
    setActionNote('')
  }

  const stats = useMemo(() => [
    { label: '快照记录总数', value: total },
    { label: '已完成快照', value: rows.filter((row) => row['快照状态'] === '已完成').length },
    { label: '待清理快照', value: rows.filter((row) => row['快照状态'] === '已完成').length },
    { label: '草稿/校验未通过', value: rows.filter((row) => row['快照状态'] === '草稿').length },
  ], [rows, total])

  return (
    <section className="page" data-module="snapshot">
      <header className="page-head">
        <div>
          <h2>快照管理</h2>
          <p className="page-desc">按恢复校验阶段推进快照状态：草稿 → 运行中 → 已完成 → 已清理；只有校验通过的已完成快照可被重跑回滚引用。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate(true)}>登记快照记录</button>
          <button className="btn" type="button" onClick={() => setShowEvidence(true)}>恢复校验结果</button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>导出快照管理清单</button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可回滚</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '快照状态'
                    ? <Badge tone={statusTone(String(row[column] ?? ''))} text={String(row[column] ?? '—')} />
                    : (row[column] ? String(row[column]) : '—')}
                </td>
              ))}
              <td>
                {row['可回滚']
                  ? <Badge tone="ok" text={`可回滚 · ${String(row['回滚依据校验编号'] ?? '')}`} />
                  : <Badge tone="no" text="不可回滚" />}
              </td>
              <td className="row-actions">
                {ACTIONS_BY_STATUS[String(row['快照状态'])]?.map((action) => (
                  <button
                    key={action}
                    className="link"
                    type="button"
                    disabled={action === '重试存储校验' && row['存储校验'] !== '失败'}
                    onClick={() => {
                      if (action === '校验通过' || action === '校验未通过') {
                        openActionDialog(action, row)
                      } else {
                        void runAction(action, row)
                      }
                    }}
                  >
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无快照数据，可先登记快照记录</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条快照记录</span>
        {notice && <span className="success-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {showCreate && (
        <Modal title="登记快照记录（同快照编号重复提交按幂等处理）" onClose={() => setShowCreate(false)}>
          <div className="form-grid">
            {CREATE_FIELDS.map((field) => (
              <label key={field.key} className={field.key === '表名称' ? 'full' : ''}>
                <span>{field.key}{field.required && ' *'}</span>
                <input
                  value={createForm[field.key] ?? ''}
                  placeholder={field.placeholder}
                  onChange={(event) => setCreateForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
                />
              </label>
            ))}
            <label className="full">
              <span>故障演练：模拟存储校验失败（用于验证失败续跑，不勾选则校验通过）</span>
              <input
                type="checkbox"
                checked={simulateStorageFail}
                onChange={(event) => setSimulateStorageFail(event.target.checked)}
              />
            </label>
          </div>
          <div className="modal-foot">
            <button className="btn ghost" type="button" onClick={() => setShowCreate(false)}>取消</button>
            <button className="btn primary" type="button" onClick={() => void submitCreate()}>创建快照</button>
          </div>
        </Modal>
      )}

      {dialog && (
        <Modal
          title={dialog.action === '校验未通过' ? '提交恢复校验未通过证据' : '提交恢复校验通过结论'}
          onClose={() => setDialog(null)}
        >
          <p className="page-desc">
            快照 {String(dialog.row['快照编号'])}：{dialog.action === '校验未通过'
              ? '未通过将保持草稿状态并保留失败证据，不能进入已完成。'
              : '通过后转为已完成，才允许被重跑引用与回滚。'}
          </p>
          <div className="form-grid">
            <label className="full">
              <span>{dialog.action === '校验未通过' ? '失败证据' : '校验结论'}</span>
              <textarea
                rows={3}
                value={actionNote}
                placeholder={dialog.action === '校验未通过'
                  ? '例如：抽样 200 行中 3 行与源表不一致'
                  : '例如：抽样 1000 行与源表完全一致'}
                onChange={(event) => setActionNote(event.target.value)}
              />
            </label>
          </div>
          <div className="modal-foot">
            <button className="btn ghost" type="button" onClick={() => setDialog(null)}>取消</button>
            <button
              className="btn primary"
              type="button"
              onClick={() => void runAction(
                dialog.action,
                dialog.row,
                dialog.action === '校验未通过' ? { 失败证据: actionNote } : { 校验结论: actionNote },
              )}
            >
              确认{dialog.action === '校验未通过' ? '未通过' : '通过'}
            </button>
          </div>
        </Modal>
      )}

      {showEvidence && (
        <Modal title="恢复校验结果（回滚依据 = 最近一次通过结论）" onClose={() => setShowEvidence(false)}>
          <div className="evidence-list">
            {evidence.length === 0 && <p className="page-desc">暂无校验记录。</p>}
            {evidence.map((item) => (
              <article className="evidence-card" key={String(item['校验编号'])}>
                <dl>
                  <dt>校验编号</dt><dd>{String(item['校验编号'])}</dd>
                  <dt>快照编号</dt><dd>{String(item['快照编号'])}</dd>
                  <dt>保留天数</dt><dd>{String(item['保留天数'] ?? '—')}</dd>
                  <dt>快照状态</dt><dd>{String(item['快照状态'])}</dd>
                  <dt>校验结果</dt>
                  <dd>{item['校验结果'] === '通过'
                    ? <Badge tone="ok" text="通过" />
                    : <Badge tone="no" text="未通过" />}</dd>
                  <dt>校验结论</dt><dd>{String(item['校验结论'])}</dd>
                  <dt>校验时间</dt><dd>{String(item['校验时间'])}</dd>
                  <dt>引用批次</dt><dd>{String(item['引用批次'] ?? '—') || '—'}</dd>
                  <dt>回滚依据</dt>
                  <dd>{item['可回滚依据']
                    ? <Badge tone="ok" text="当前回滚依据" />
                    : <Badge tone="no" text="不作为回滚依据" />}</dd>
                </dl>
              </article>
            ))}
          </div>
        </Modal>
      )}
    </section>
  )
}
