"use client";

import { ColorType, createChart, LineSeries, LineStyle, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { fmtUtc } from "@/lib/format";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Equity curve of the primary run: close-marked equity and the conservative intrabar mark. */
export default function EquityChart({ points, initial }: { points: [string, string, string][]; initial: number }) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: cssVar("--card") },
        textColor: cssVar("--muted"),
        fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
        attributionLogo: true,
      },
      grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: cssVar("--grid") },
      rightPriceScale: { borderColor: cssVar("--grid") },
      localization: {
        locale: "en-US",
        timeFormatter: (t: Time) => `${fmtUtc(new Date((t as number) * 1000).toISOString())} UTC`,
      },
    });
    const worst = c.addSeries(LineSeries, {
      color: cssVar("--down"),
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    const eq = c.addSeries(LineSeries, { color: cssVar("--accent"), lineWidth: 2, priceLineVisible: false });
    eq.createPriceLine({ price: initial, color: cssVar("--muted"), lineStyle: LineStyle.Dashed, lineWidth: 1, title: "הון התחלתי" });
    const t = (iso: string) => Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
    eq.setData(points.map((p) => ({ time: t(p[0]), value: +p[1] })));
    worst.setData(points.map((p) => ({ time: t(p[0]), value: +p[2] })));
    c.timeScale().fitContent();
    return () => c.remove();
  }, [points, initial]);

  return (
    <div>
      <div dir="ltr" className="chart-box-sm" ref={box} role="img" aria-label="עקומת הון וירטואלית" />
      <p className="mt-2 text-xs muted">
        כחול = הון לפי מחיר סגירה של כל נר · אדום מנוקד = סימון שמרני לפי המחיר הגרוע ביותר בתוך הנר (משמש לחישוב
        Drawdown).
      </p>
    </div>
  );
}
