import { useQuery } from '@tanstack/react-query'
import { getQuota } from '@/lib/api'

/**
 * Compact remaining-quota gauge for the active provider's subscription.
 *
 * Shown in the sidebar under the project selector. Only rendered when the
 * active provider exposes rate-limit windows (currently the Codex engine).
 */
export function QuotaBadge() {
  const { data } = useQuery({
    queryKey: ['provider-quota'],
    queryFn: getQuota,
    refetchInterval: 90_000,
    staleTime: 60_000,
    retry: false,
  })

  if (!data?.supported || data.windows.length === 0) return null

  return (
    <div className="mt-2 space-y-1.5">
      {data.windows.map((w) => {
        const pct = Math.min(100, Math.max(0, w.used_percent))
        const barColor =
          pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-amber-500' : 'bg-emerald-500'
        const resetStr = w.resets_at
          ? new Date(w.resets_at * 1000).toLocaleString([], {
              day: '2-digit',
              month: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
            })
          : null
        return (
          <div key={w.name} title={resetStr ? `Resets ${resetStr}` : undefined}>
            <div className="flex justify-between text-[10px] text-sidebar-foreground/60 mb-0.5">
              <span className="uppercase tracking-wide">
                {data.provider} · {w.name}
              </span>
              <span>{pct}% used</span>
            </div>
            <div className="h-1 rounded bg-sidebar-border overflow-hidden">
              <div className={barColor} style={{ width: `${pct}%`, height: '100%' }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}
