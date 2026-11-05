"use client";

import { ColorType, createChart, HistogramSeries, LineSeries, LineStyle, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { fmtUtc } from "@/lib/format";
import type { EquityCurve } from "@/lib/paper";

/** Virtual account equity (5-minute snapshots by the paper worker) and daily P&L. */
export default function EquityPanel({ curve }: { curve: EquityCurve | null }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current || !curve || curve.points.length === 0) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b0e14" }, textColor: "#8b93a7", attributionLogo: true },
      grid: { vertLines: { color: "#161b26" }, horzLines: { color: "#161b26" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#161b26" },
      rightPriceScale: { borderColor: "#161b26" },
      localization: {
        locale: "en-US",
        timeFormatter: (t: Time) => `${fmtUtc(new Date((t as number) * 1000).toISOString())} UTC`,
      },
    });
    const init = Number(curve.initial_equity);
    const eq = c.addSeries(LineSeries, { color: "#60a5fa", lineWidth: 2, priceLineVisible: false });
    eq.createPriceLine({ price: init, color: "#6b7280", lineStyle: LineStyle.Dashed, lineWidth: 1, title: "1,000" });
    eq.setData(curve.points.map((p) => ({ time: (Date.parse(p[0]) / 1000) as UTCTimestamp, value: +p[1] })));
    const daily = c.addSeries(HistogramSeries, { priceScaleId: "d", priceLineVisible: false, lastValueVisible: false }, 1);
    daily.setData(
      curve.daily_pnl.map(([d, v]) => ({
        time: (Date.parse(`${d}T00:00:00Z`) / 1000) as UTCTimestamp,
        value: +v,
        color: +v >= 0 ? "#22c55e" : "#ef4444",
      })),
    );
    const panes = c.panes();
    if (panes[1]) panes[1].setHeight(70);
    c.timeScale().fitContent();
    return () => c.remove();
  }, [curve]);

  return (
    <section className="t-card" aria-labelledby="t-eq">
      <h2 id="t-eq" className="mb-2 text-sm font-semibold">עקומת הון וירטואלית ורווח/הפסד יומי</h2>
      {curve && curve.points.length > 0 ? (
        <div dir="ltr" className="t-chart-sm" ref={box} role="img" aria-label="עקומת הון" />
      ) : (
        <p className="text-sm t-muted">אין עדיין נקודות הון. הן נרשמות כל 5 דקות בזמן שהבוט פועל.</p>
      )}
      <p className="mt-2 text-[11px] t-muted">מקור: צילומי הון של מנוע ה־PAPER (לא backtest). יום = יום קלנדרי UTC.</p>
    </section>
  );
}
