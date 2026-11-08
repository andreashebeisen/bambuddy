import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, Loader2, X, XCircle } from 'lucide-react';
import { api, ApiError } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { formatFileSize } from '../utils/file';

type Outcome = 'imported' | 'duplicate' | 'failed';

/**
 * Import the sliced files on the printers' storage that no archive holds yet:
 * a print sent from Bambu Cloud leaves its .gcode.3mf on the printer, not in
 * Bambuddy. Scans, lists the new files (all ticked), then imports them one by
 * one with their outcome.
 */
export function PrinterImportModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const scan = useQuery({
    queryKey: ['printer-import-scan'],
    queryFn: () => api.scanPrinterImport(),
    gcTime: 0,
    retry: false,
  });

  const key = (printerId: number, path: string) => `${printerId}:${path}`;
  const allKeys = useMemo(
    () => (scan.data?.printers ?? []).flatMap((p) => p.files.map((f) => key(p.printer_id, f.path))),
    [scan.data],
  );
  const [picked, setPicked] = useState<Set<string>>(new Set());
  useEffect(() => setPicked(new Set(allKeys)), [allKeys]);

  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const [running, setRunning] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const busy = running !== null;

  const toggle = (k: string) =>
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const runImport = async () => {
    const jobs = (scan.data?.printers ?? []).flatMap((p) =>
      p.files.filter((f) => picked.has(key(p.printer_id, f.path))).map((f) => ({ printerId: p.printer_id, path: f.path })),
    );
    let imported = 0;
    let duplicates = 0;
    let failed = 0;
    for (const job of jobs) {
      const k = key(job.printerId, job.path);
      setRunning(k);
      try {
        const result = await api.importPrinterFile(job.printerId, job.path);
        setOutcomes((o) => ({ ...o, [k]: result.status }));
        if (result.status === 'imported') imported++;
        else duplicates++;
      } catch (error) {
        setOutcomes((o) => ({ ...o, [k]: 'failed' }));
        failed++;
        if (!(error instanceof ApiError)) console.error(error);
      }
    }
    setRunning(null);
    setDone(true);
    queryClient.invalidateQueries({ queryKey: ['archives'] });
    queryClient.invalidateQueries({ queryKey: ['archive-merge-count'] });
    showToast(
      t('printerImport.toast.done', { imported, duplicates, failed }),
      failed > 0 ? 'warning' : 'success',
    );
  };

  const printers = scan.data?.printers ?? [];
  const totalNew = allKeys.length;

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" data-testid="printer-import-modal">
      <div className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-2xl max-h-[90vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-white font-semibold">{t('printerImport.title')}</h3>
          <button onClick={onClose} disabled={busy} className="text-bambu-gray hover:text-white transition-colors" title={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 overflow-y-auto space-y-4 text-sm">
          {scan.isLoading ? (
            <div className="flex flex-col items-center gap-2 py-8 text-bambu-gray">
              <Loader2 className="w-6 h-6 animate-spin text-bambu-green" />
              {t('printerImport.scanning')}
            </div>
          ) : scan.isError ? (
            <p className="text-red-400">{t('printerImport.scanFailed')}</p>
          ) : printers.length === 0 ? (
            <p className="text-bambu-gray">{t('printerImport.noPrinters')}</p>
          ) : (
            <>
              <p className="text-bambu-gray">{t('printerImport.intro')}</p>
              {totalNew > 0 && (
                <label className="flex items-center gap-3 px-3 py-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary cursor-pointer">
                  <input
                    type="checkbox"
                    ref={(el) => {
                      if (el) el.indeterminate = picked.size > 0 && picked.size < totalNew;
                    }}
                    checked={picked.size === totalNew}
                    onChange={() => setPicked(picked.size === totalNew ? new Set() : new Set(allKeys))}
                    disabled={busy || done}
                    className="accent-bambu-green"
                  />
                  <span className="text-white">{t('printerImport.selectAll')}</span>
                  <span className="ml-auto text-xs text-bambu-gray">
                    {t('printerImport.selected', { count: picked.size, total: totalNew })}
                  </span>
                </label>
              )}
              {printers.map((p) => (
                <div key={p.printer_id} className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-white font-medium">{p.name}</span>
                    <span className="text-xs text-bambu-gray">
                      {p.available
                        ? t('printerImport.counts', { count: p.files.length, imported: p.already_imported })
                        : ''}
                    </span>
                  </div>
                  {!p.available ? (
                    <p className="flex items-center gap-1.5 text-amber-400 text-xs">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      {t('printerImport.unavailable')}
                    </p>
                  ) : p.files.length === 0 ? (
                    <p className="text-xs text-bambu-gray">{t('printerImport.nothingNew')}</p>
                  ) : (
                    <ul className="divide-y divide-bambu-dark-tertiary border border-bambu-dark-tertiary rounded-lg">
                      {p.files.map((f) => {
                        const k = key(p.printer_id, f.path);
                        const outcome = outcomes[k];
                        return (
                          <li key={k} className="flex items-center gap-3 px-3 py-2">
                            <input
                              type="checkbox"
                              checked={picked.has(k)}
                              onChange={() => toggle(k)}
                              disabled={busy || done}
                              aria-label={f.name}
                              className="accent-bambu-green"
                            />
                            <div className="min-w-0 flex-1">
                              <div className="text-white truncate" title={f.path}>{f.name}</div>
                              <div className="text-xs text-bambu-gray truncate">
                                {f.path}
                                {f.size != null && ` · ${formatFileSize(f.size)}`}
                                {f.mtime && ` · ${new Date(f.mtime).toLocaleString()}`}
                              </div>
                            </div>
                            <span className="shrink-0 text-xs">
                              {running === k && <Loader2 className="w-4 h-4 animate-spin text-bambu-green" />}
                              {outcome === 'imported' && (
                                <span className="flex items-center gap-1 text-bambu-green"><CheckCircle2 className="w-4 h-4" />{t('printerImport.outcome.imported')}</span>
                              )}
                              {outcome === 'duplicate' && (
                                <span className="text-bambu-gray">{t('printerImport.outcome.duplicate')}</span>
                              )}
                              {outcome === 'failed' && (
                                <span className="flex items-center gap-1 text-red-400"><XCircle className="w-4 h-4" />{t('printerImport.outcome.failed')}</span>
                              )}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              ))}
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {done ? t('common.close') : t('common.cancel')}
          </Button>
          {!done && totalNew > 0 && (
            <Button onClick={runImport} disabled={busy || picked.size === 0}>
              {busy && <Loader2 className="w-4 h-4 animate-spin" />}
              {busy ? t('printerImport.importing') : t('printerImport.importCta', { count: picked.size })}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
