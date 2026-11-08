import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, X } from 'lucide-react';
import { api, ApiError } from '../api/client';
import type { InventorySpool, RunSpoolChargeFilament } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

/**
 * Charge a print's spools by hand: a cloud print whose 3MF reached Bambuddy
 * only later (uploaded and merged), so nothing was charged when it ended.
 * Each filament of the file comes with a suggested spool; the user confirms
 * the spool and grams, and scales them down for a print that stopped early.
 */
export function SpoolChargeDialog({
  archiveId,
  runId,
  onClose,
}: {
  archiveId: number;
  runId: number;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const proposal = useQuery({
    queryKey: ['run-spool-charge', archiveId, runId],
    queryFn: () => api.getRunSpoolCharge(archiveId, runId),
    retry: false,
  });
  const spools = useQuery({
    queryKey: ['inventory-spools', 'spool-charge'],
    queryFn: () => api.getSpools(false),
  });

  const [percent, setPercent] = useState(100);
  const [rows, setRows] = useState<{ spoolId: number | null; grams: string }[]>([]);

  const filaments: RunSpoolChargeFilament[] = useMemo(() => proposal.data?.filaments ?? [], [proposal.data]);
  useEffect(() => {
    setRows(filaments.map((f) => ({ spoolId: f.spool_id, grams: f.grams.toFixed(1) })));
  }, [filaments]);

  const scale = (value: number) => {
    const clamped = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
    setPercent(clamped);
    setRows((current) =>
      current.map((row, i) => ({ ...row, grams: ((filaments[i]?.grams ?? 0) * clamped / 100).toFixed(1) })),
    );
  };

  const lines = rows
    .map((row) => ({ spool_id: row.spoolId, grams: parseFloat(row.grams) }))
    .filter((line): line is { spool_id: number; grams: number } => line.spool_id != null && line.grams > 0);

  const mutation = useMutation({
    mutationFn: () => api.chargeRunSpools(archiveId, runId, lines),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['archive-runs', archiveId] });
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['archive', archiveId] });
      queryClient.invalidateQueries({ queryKey: ['print-log'] });
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spools'] });
      const grams = data.charged.reduce((sum, c) => sum + c.weight_used, 0);
      showToast(t('spoolCharge.toast.charged', { count: data.charged.length, grams: grams.toFixed(1) }), 'success');
      onClose();
    },
    onError: (error) => {
      const code = error instanceof ApiError ? (error.detail?.code as string | undefined) : undefined;
      showToast(code ? t(`spoolCharge.error.${code}`, { defaultValue: t('spoolCharge.toast.failed') }) : t('spoolCharge.toast.failed'), 'error');
    },
  });

  const errorCode = proposal.error instanceof ApiError ? (proposal.error.detail?.code as string | undefined) : undefined;
  const spoolLabel = (s: InventorySpool) => {
    const remaining = Math.max(0, s.label_weight - s.weight_used);
    return `${[s.brand, s.material, s.subtype, s.color_name].filter(Boolean).join(' ')} · ${remaining.toFixed(0)} g (#${s.id})`;
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" data-testid="spool-charge-dialog">
      <div className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-2xl max-h-[90vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-white font-semibold">{t('spoolCharge.title')}</h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors" title={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 overflow-y-auto space-y-4 text-sm">
          {proposal.isLoading || spools.isLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-bambu-green" />
            </div>
          ) : proposal.isError ? (
            <p className="text-red-400">{t(`spoolCharge.error.${errorCode ?? 'load'}`, { defaultValue: t('spoolCharge.error.load') })}</p>
          ) : proposal.data?.already_charged ? (
            <p className="text-amber-400">{t('spoolCharge.error.already_charged')}</p>
          ) : filaments.length === 0 ? (
            <p className="text-bambu-gray">{t('spoolCharge.noFilaments')}</p>
          ) : (
            <>
              <p className="text-bambu-gray">{t('spoolCharge.intro')}</p>
              <label className="flex items-center gap-3">
                <span className="text-bambu-gray-light">{t('spoolCharge.percent')}</span>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  value={percent}
                  onChange={(e) => scale(parseFloat(e.target.value))}
                  className="w-20 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
                  aria-label={t('spoolCharge.percent')}
                />
                <span className="text-bambu-gray">%</span>
              </label>
              <div className="space-y-3">
                {filaments.map((f, i) => (
                  <div key={f.slot_id} className="grid grid-cols-1 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)_6rem] gap-2 items-center" data-testid={`spool-charge-line-${f.slot_id}`}>
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className="w-4 h-4 rounded-full border border-black/30 shrink-0"
                        style={{ backgroundColor: f.color ?? undefined }}
                        title={f.color ?? undefined}
                      />
                      <div className="min-w-0">
                        <div className="text-white truncate">
                          {f.material ?? '—'} · {f.grams.toFixed(1)} g
                        </div>
                        <div className="text-xs text-bambu-gray truncate">
                          {f.ams_id != null && f.tray_id != null
                            ? t('spoolCharge.slot', { ams: f.ams_id >= 128 ? f.ams_id : f.ams_id + 1, tray: f.tray_id + 1 })
                            : t('spoolCharge.filamentSlot', { slot: f.slot_id })}
                          {f.match && ` · ${t(`spoolCharge.match.${f.match}`)}`}
                        </div>
                      </div>
                    </div>
                    <select
                      value={rows[i]?.spoolId ?? ''}
                      onChange={(e) =>
                        setRows((current) =>
                          current.map((row, j) => (j === i ? { ...row, spoolId: e.target.value ? Number(e.target.value) : null } : row)),
                        )
                      }
                      className="w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
                      aria-label={t('spoolCharge.spoolFor', { slot: f.slot_id })}
                    >
                      <option value="">{t('spoolCharge.noSpool')}</option>
                      {(spools.data ?? []).map((s) => (
                        <option key={s.id} value={s.id}>
                          {spoolLabel(s)}
                        </option>
                      ))}
                    </select>
                    <label className="flex items-center gap-1">
                      <input
                        type="number"
                        min={0}
                        step={0.1}
                        value={rows[i]?.grams ?? ''}
                        onChange={(e) =>
                          setRows((current) => current.map((row, j) => (j === i ? { ...row, grams: e.target.value } : row)))
                        }
                        className="w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
                        aria-label={t('spoolCharge.gramsFor', { slot: f.slot_id })}
                      />
                      <span className="text-bambu-gray">g</span>
                    </label>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          {proposal.data && !proposal.data.already_charged && filaments.length > 0 && (
            <Button onClick={() => mutation.mutate()} disabled={lines.length === 0 || mutation.isPending}>
              {mutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
              {t('spoolCharge.confirm', { grams: lines.reduce((sum, l) => sum + l.grams, 0).toFixed(1) })}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
