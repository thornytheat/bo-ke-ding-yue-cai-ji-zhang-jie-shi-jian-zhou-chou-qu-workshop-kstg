import { useState } from 'react';
import StatusPill from './StatusPill';
import TapeLabel from './TapeLabel';
import EmptyState from './EmptyState';
import { exportApi, EXPORT_FILES } from '../../api/exports';
import { usePolling } from '../../hooks/usePolling';
import { EXPORT_PACKAGE_STATUS_LABELS, exportStatusTone } from '../../constants/enums';
import { errorMessage } from '../../utils/errorMessage';
import { humanDuration, relativeTime } from '../../utils/format';
import { uiStore } from '../../stores/uiStore';
import type { ExportFileManifest, ExportPackage } from '../../types/models';

interface PackageManagerProps {
  packages: ExportPackage[];
  loading: boolean;
  reload: () => void;
}

const FIELD_LABELS: Record<string, string> = {
  duration_s: '时长(s)',
  chapter_rows: '章节行数',
  unparsed_rows: '未识别行数',
  open_issues: '待处理异常',
  resolved_issues: '已处置异常',
  parse_status: '解析状态',
};

function RowCounts({ pkg }: { pkg: ExportPackage }) {
  const files: { key: string; name: string }[] = [
    { key: EXPORT_FILES.clipList, name: '剪辑清单' },
    { key: EXPORT_FILES.chapterCuts, name: '章节切点' },
    { key: EXPORT_FILES.issues, name: '异常清单' },
  ];
  return (
    <div className="pkg-rows">
      {files.map((file) => {
        const meta = pkg.manifest?.[file.key];
        return (
          <div className="pkg-row-count" key={file.key}>
            <span className="pkg-row-name">{file.name}</span>
            <span className="mono pkg-row-num">{meta ? meta.data_rows : '—'}</span>
            <span className="pkg-row-unit">行</span>
          </div>
        );
      })}
    </div>
  );
}

function DiffView({ pkg }: { pkg: ExportPackage }) {
  const diff = pkg.diff;
  if (!diff || !diff.against) {
    return <p className="muted small pkg-diff-empty">首个封版包，暂无历史版本可比对。</p>;
  }
  const { summary } = diff;
  const changedTotal =
    diff.episodes_added.length + diff.episodes_removed.length + diff.episodes_changed.length;
  return (
    <div className="pkg-diff">
      <div className="pkg-diff-summary mono">
        <span>相对 {diff.against}</span>
        <Delta label="集数" value={summary.episodes} />
        <Delta label="章节" value={summary.chapters} />
        <Delta label="异常" value={summary.issues} />
        <Delta label="时长(s)" value={summary.total_duration_s} />
      </div>
      {changedTotal === 0 ? (
        <p className="muted small">口径与上一封版包一致（仅重新封版）。</p>
      ) : (
        <ul className="pkg-diff-list">
          {diff.episodes_added.map((code) => (
            <li key={`add-${code}`}>
              <StatusPill tone="ok">新增</StatusPill> <span className="mono">{code}</span>
            </li>
          ))}
          {diff.episodes_removed.map((code) => (
            <li key={`rm-${code}`}>
              <StatusPill tone="bad">移除</StatusPill> <span className="mono">{code}</span>
            </li>
          ))}
          {diff.episodes_changed.map((item) => (
            <li key={`chg-${item.episode_code}`}>
              <StatusPill tone="warn">变更</StatusPill>
              <span className="mono">{item.episode_code}</span>
              <span className="muted"> {item.title}</span>
              <span className="pkg-diff-fields">
                {Object.entries(item.fields).map(([field, change]) => (
                  <span className="pkg-diff-field mono" key={field}>
                    {FIELD_LABELS[field] ?? field}: {String(change.from)} → {String(change.to)}
                  </span>
                ))}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Delta({ label, value }: { label: string; value: number }) {
  if (value === 0) {
    return (
      <span className="pkg-delta pkg-delta-flat">
        {label} ±0
      </span>
    );
  }
  const positive = value > 0;
  return (
    <span className={`pkg-delta ${positive ? 'pkg-delta-up' : 'pkg-delta-down'}`}>
      {label} {positive ? '+' : ''}
      {value}
    </span>
  );
}

export default function PackageManager({ packages, loading, reload }: PackageManagerProps) {
  const [busyCode, setBusyCode] = useState<string | null>(null);
  const [openCode, setOpenCode] = useState<string | null>(null);

  const hasBuilding = packages.some((pkg) => pkg.status === 'building');
  usePolling(reload, 1500, hasBuilding);

  const runDownload = async (code: string, action: () => Promise<number>, label: string) => {
    setBusyCode(code);
    try {
      const bytes = await action();
      uiStore.toast(`${code} · ${label}已下载（${Math.max(1, Math.round(bytes / 1024))} KB）`);
    } catch (error) {
      uiStore.toast(errorMessage((error as { payload?: unknown })?.payload, '下载失败'), 'bad');
    } finally {
      setBusyCode(null);
    }
  };

  if (!loading && packages.length === 0) {
    return (
      <EmptyState
        title="还没有发布包"
        hint="点「发布不可变剪辑包」冻结当前解析口径，核对一致后才会开放下载。"
      />
    );
  }

  return (
    <div className="pkg-list">
      {packages.map((pkg) => {
        const sealed = pkg.status === 'sealed';
        const expanded = openCode === pkg.code;
        return (
          <article
            className={`pkg-card ${pkg.is_current ? 'pkg-current' : ''} pkg-${pkg.status}`}
            key={pkg.code}
          >
            <div className="pkg-card-head">
              <div className="pkg-id">
                <span className="eyebrow">{pkg.is_current ? 'CURRENT · 当前包' : 'HISTORY · 历史包'}</span>
                <h3 className="pkg-code mono">{pkg.code}</h3>
                <StatusPill tone={exportStatusTone(pkg.status)} dot={pkg.status === 'building'}>
                  {EXPORT_PACKAGE_STATUS_LABELS[pkg.status] ?? pkg.status}
                </StatusPill>
              </div>
              <div className="pkg-meta mono">
                <span title="冻结的解析批次">批次 {pkg.digest_task_code || '—'}</span>
                <span title="总时长">总时长 {humanDuration(pkg.total_duration_s)}</span>
                <span title="封版时间">{relativeTime(pkg.sealed_at ?? pkg.created_at)}</span>
                <span title="发起人">{pkg.created_by || '—'}</span>
              </div>
            </div>

            {pkg.status === 'failed' && (
              <p className="pkg-error">构建失败已废弃：{pkg.error_text || '未知原因'}（可重新发起，不会产生半包）</p>
            )}

            <RowCounts pkg={pkg} />

            <div className="pkg-card-foot">
              <div className="pkg-actions">
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={!sealed || busyCode === pkg.code}
                  onClick={() =>
                    runDownload(pkg.code, () => exportApi.downloadZip(pkg.code), '整包 ZIP ')
                  }
                  title={sealed ? '下载含三份 CSV + manifest 的不可变整包' : '封版后才开放下载'}
                >
                  下载整包 ZIP
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={!sealed || busyCode === pkg.code}
                  onClick={() =>
                    runDownload(
                      pkg.code,
                      () => exportApi.downloadFile(pkg.code, EXPORT_FILES.clipList),
                      '剪辑清单',
                    )
                  }
                >
                  剪辑清单
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={!sealed || busyCode === pkg.code}
                  onClick={() =>
                    runDownload(
                      pkg.code,
                      () => exportApi.downloadFile(pkg.code, EXPORT_FILES.chapterCuts),
                      '章节切点',
                    )
                  }
                >
                  章节切点
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={!sealed || busyCode === pkg.code}
                  onClick={() =>
                    runDownload(
                      pkg.code,
                      () => exportApi.downloadFile(pkg.code, EXPORT_FILES.issues),
                      '异常清单',
                    )
                  }
                >
                  异常清单
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  onClick={() => setOpenCode(expanded ? null : pkg.code)}
                >
                  {expanded ? '收起差异' : '查看差异 / 校验'}
                </button>
              </div>
              {sealed && (
                <span className="pkg-sha mono" title={pkg.zip_sha256}>
                  sha256:{pkg.zip_sha256.slice(0, 12)}… · {Math.round(pkg.zip_bytes / 1024)}KB
                </span>
              )}
            </div>

            {expanded && (
              <div className="pkg-detail">
                <TapeLabel eyebrow="DIFF · 版本差异" title="与上一封版包的口径差异" tone="plain">
                  <DiffView pkg={pkg} />
                </TapeLabel>
                <div className="pkg-checksum mono">
                  {Object.entries(pkg.manifest ?? {}).map(([name, meta]) => (
                    <div className="pkg-checksum-row" key={name}>
                      <span className="pkg-checksum-name">{name}</span>
                      <span>{meta.data_rows} 行</span>
                      <span>{meta.bytes} B</span>
                      <span title={meta.sha256}>sha {meta.sha256.slice(0, 10)}…</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
