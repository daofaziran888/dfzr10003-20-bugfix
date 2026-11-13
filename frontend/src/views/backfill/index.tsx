import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { postAction, request } from '@/api/client'

const ENDPOINT = '/api/backfill'

const columns = [
  '批次编号',
  '目标表名',
  '开始日期',
  '结束日期',
  '并行数量',
  '覆盖模式',
  '重跑状态',
  '异常区间',
  '引用快照编号',
  '引用快照状态',
  '引用保留天数',
  '引用恢复校验',
  '回滚依据校验编号',
]

/** 与后端 BackfillService.ACTION_RULES 对齐，动作按状态受控出现。 */
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  草稿: ['启动重跑', '引用快照'],
  运行中: ['暂停重跑', '完成重跑'],
  已暂停: ['恢复重跑', '引用快照'],
  已完成: ['归档批次'],
}

const CREATE_FIELDS = [
  { key: '批次编号', required: true, placeholder: '例如 BACKFILL-0004' },
  { key: '目标表名', required: true, placeholder: '例如 dws_order_summary' },
  { key: '开始日期', required: true, placeholder: 'YYYY-MM-DD' },
  { key: '结束日期', required: false, placeholder: 'YYYY-MM-DD' },
  { key: '并行数量', required: false, placeholder: '例如 20' },
  { key: '覆盖模式', required: false, placeholder: '增量合并 / 整分区覆盖' },
]

type Row = Record<string, string | number | boolean | null>

function Badge({ tone, text }: { tone: 'ok' | 'no' | 'info' | 'warn'; text: string }) {
  return <span className={`badge badge-${tone}`}>{text}</span>
}

function statusTone(status: string): 'ok' | 'no' | 'info' | 'warn' {
  if (status === '已完成') return 'ok'
  if (status === '运行中') return 'info'
  if (status === '已暂停') return 'warn'
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

export default function BackfillView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState<Record<string, string>>({})
  const [refTarget, setRefTarget] = useState<Row | null>(null)
  const [snapshotCode, setSnapshotCode] = useState('')
  const filterFields = useMemo(() => ['批次编号', '重跑状态'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    // 后端列表接口按 keyword/status 过滤，中文筛选字段在这里统一映射，避免页面各传各的。
    const params = new URLSearchParams()
    if (filters['批次编号']) params.set('keyword', filters['批次编号'])
    if (filters['重跑状态']) params.set('status', filters['重跑状态'])
    const query = params.toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('重跑批次列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '历史重跑列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row, extra: Record<string, unknown> = {}) {
    setErrorMessage('')
    setNotice('')
    const outcome = await postAction(`${ENDPOINT}/${row.id}/actions`, { action, ...extra })
    if (!outcome.ok) {
      setErrorMessage(outcome.message)
    } else {
      setNotice(outcome.message)
    }
    setRefTarget(null)
    setSnapshotCode('')
    await reload()
  }

  async function submitCreate() {
    setErrorMessage('')
    setNotice('')
    const outcome = await postAction(ENDPOINT, createForm)
    if (!outcome.ok) {
      setErrorMessage(outcome.message)
      return
    }
    setNotice(outcome.message)
    setShowCreate(false)
    setCreateForm({})
    await reload()
  }

  const stats = useMemo(() => [
    { label: '重跑批次总数', value: total },
    { label: '运行中批次', value: rows.filter((row) => row['重跑状态'] === '运行中').length },
    { label: '已暂停批次', value: rows.filter((row) => row['重跑状态'] === '已暂停').length },
    { label: '持有有效回滚引用', value: rows.filter((row) => row['可回滚']).length },
  ], [rows, total])

  return (
    <section className="page" data-module="backfill">
      <header className="page-head">
        <div>
          <h2>历史重跑管理</h2>
          <p className="page-desc">只有恢复校验通过的已完成快照才能被重跑引用；引用状态、保留期与回滚证据与快照页实时同源，已清理快照不会再显示为可回滚。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate(true)}>登记重跑批次</button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>导出历史重跑清单</button>
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
                  {column === '重跑状态' || column === '引用快照状态'
                    ? (row[column]
                      ? <Badge tone={statusTone(String(row[column]))} text={String(row[column])} />
                      : '—')
                    : (row[column] ? String(row[column]) : '—')}
                </td>
              ))}
              <td>
                {row['可回滚']
                  ? <Badge tone="ok" text={`可回滚 · ${String(row['回滚依据校验编号'] ?? '')}`} />
                  : <Badge tone="no" text="不可回滚" />}
              </td>
              <td className="row-actions">
                {ACTIONS_BY_STATUS[String(row['重跑状态'])]?.map((action) => (
                  <button
                    key={action}
                    className="link"
                    type="button"
                    onClick={() => {
                      if (action === '引用快照') {
                        setRefTarget(row)
                        setSnapshotCode(String(row['引用快照编号'] ?? ''))
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
              <td className="empty-state" colSpan={columns.length + 2}>暂无历史重跑数据，可先登记重跑批次</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条历史重跑记录</span>
        {notice && <span className="success-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {showCreate && (
        <Modal title="登记重跑批次（同批次编号重复提交按幂等返回原记录）" onClose={() => setShowCreate(false)}>
          <div className="form-grid">
            {CREATE_FIELDS.map((field) => (
              <label key={field.key}>
                <span>{field.key}{field.required && ' *'}</span>
                <input
                  value={createForm[field.key] ?? ''}
                  placeholder={field.placeholder}
                  onChange={(event) => setCreateForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="modal-foot">
            <button className="btn ghost" type="button" onClick={() => setShowCreate(false)}>取消</button>
            <button className="btn primary" type="button" onClick={() => void submitCreate()}>登记批次</button>
          </div>
        </Modal>
      )}

      {refTarget && (
        <Modal title={`为批次 ${String(refTarget['批次编号'])} 引用快照`} onClose={() => setRefTarget(null)}>
          <p className="page-desc">
            仅允许引用「已完成 + 恢复校验通过」的快照；草稿、运行中、已清理或校验未通过的快照会被后端拒绝。
          </p>
          <div className="form-grid">
            <label className="full">
              <span>快照编号 *</span>
              <input value={snapshotCode} placeholder="例如 SNAP-0002" onChange={(event) => setSnapshotCode(event.target.value)} />
            </label>
          </div>
          <div className="modal-foot">
            <button className="btn ghost" type="button" onClick={() => setRefTarget(null)}>取消</button>
            <button
              className="btn primary"
              type="button"
              onClick={() => void runAction('引用快照', refTarget, { 快照编号: snapshotCode })}
            >
              确认引用
            </button>
          </div>
        </Modal>
      )}
    </section>
  )
}
