"use client";

import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { getBackend } from "@/lib/client";
import { fmtUtc } from "@/lib/format";
import { explain } from "@/lib/i18n";
import type { BtTrade, CandlesResponse } from "@/lib/types";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const ts = (iso: string) => Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;

type Props = {
  trades: BtTrade[];
  start: string;
  end: string;
  focus: number | null;
};

/** Real stored 30m candles for the run's window, with the run's entries/exits as markers. */
export default function TradeChart({ trades, start, end, focus }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const lines = useRef<ISeriesApi<"Line">[]>([]);
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    series.current = c.addSeries(CandlestickSeries, {
      upColor: cssVar("--up"),
      downColor: cssVar("--down"),
      wickUpColor: cssVar("--up"),
      wickDownColor: cssVar("--down"),
      borderVisible: false,
      priceLineVisible: false,
    });
    markers.current = createSeriesMarkers(series.current, []);
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
      lines.current = [];
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const res = await getBackend<CandlesResponse>("candles", {
        interval: "30m",
        start,
        end,
        limit: "5000",
        include_forming: "false",
      });
      if (cancelled || !series.current) return;
      if (!res.ok) {
        setError(res.error);
        return;
      }
      setError(null);
      setCount(res.data.count);
      series.current.setData(
        res.data.candles.map((k) => ({ time: k.t as UTCTimestamp, open: +k.o, high: +k.h, low: +k.l, close: +k.c })),
      );
      const up = cssVar("--up");
      const down = cssVar("--down");
      const ms: SeriesMarker<Time>[] = [];
      for (const t of trades) {
        const long = t.side === "LONG";
        ms.push({
          time: ts(t.entry_time),
          position: long ? "belowBar" : "aboveBar",
          shape: long ? "arrowUp" : "arrowDown",
          color: long ? up : down,
          text: `#${t.id} ${long ? "L" : "S"}`,
        });
        const win = Number(t.net_pnl) > 0;
        ms.push({
          time: ts(t.exit_bar_open ?? t.exit_time),
          position: long ? "aboveBar" : "belowBar",
          shape: "circle",
          color: win ? up : down,
          text: `#${t.id} ${win ? "✓" : "✗"}`,
        });
      }
      ms.sort((a, b) => (a.time as number) - (b.time as number));
      markers.current?.setMarkers(ms);
      chart.current?.timeScale().fitContent();
    })();
    return () => {
      cancelled = true;
    };
  }, [trades, start, end]);

  // Focused trade: zoom to it and draw its entry / stop / target levels.
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    for (const l of lines.current) c.removeSeries(l);
    lines.current = [];
    const t = trades.find((x) => x.id === focus);
    if (!t) return;
    const a = ts(t.entry_time);
    const b = ts(t.exit_bar_open ?? t.exit_time);
    const level = (price: string, color: string, style: LineStyle) => {
      const s = c.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        lineStyle: style,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      s.setData([
        { time: a, value: +price },
        { time: (b > a ? b : a + 1800) as UTCTimestamp, value: +price },
      ]);
      lines.current.push(s);
    };
    level(t.entry_price, cssVar("--accent"), LineStyle.Solid);
    level(t.stop, cssVar("--down"), LineStyle.Dashed);
    level(t.target, cssVar("--up"), LineStyle.Dashed);
    c.timeScale().setVisibleRange({ from: (a - 40 * 1800) as UTCTimestamp, to: (b + 40 * 1800) as UTCTimestamp });
  }, [focus, trades]);

  return (
    <div>
      <div dir="ltr" className="chart-box" ref={box} role="img" aria-label="גרף נרות 30 דקות עם נקודות כניסה ויציאה" />
      <p className="mt-2 text-xs muted">
        חץ = כניסה (L לונג / S שורט) · עיגול = יציאה (✓ רווח, ✗ הפסד) · {count != null ? `${count.toLocaleString("en-US")} נרות סגורים מהמסד` : ""}
        {focus != null ? " · קו כחול = כניסה, אדום מקווקו = סטופ, ירוק מקווקו = יעד" : " · לחיצה על עסקה בטבלה מתמקדת בה"}
      </p>
      {error && <p className="mt-1 text-sm err">{explain(error)}</p>}
    </div>
  );
}
