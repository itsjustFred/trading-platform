import { useEffect, useRef } from 'react';
import { createChart, CrosshairMode } from 'lightweight-charts';
import type { IChartApi, Time, UTCTimestamp, DeepPartial, ChartOptions } from 'lightweight-charts';

type OHLC = { time: Time | UTCTimestamp | number; open: number; high: number; low: number; close: number };
type Volume = { time: Time | UTCTimestamp | number; value: number; color?: string };
type Trade = { time: Time | UTCTimestamp | number; price: number; side: 'buy' | 'sell'; label?: string; pnl?: number };
type Indicator = { name: string; data: { time: Time | UTCTimestamp | number; value: number }[]; color?: string; panel?: 'main' | 'separate' };

interface Props {
  ohlcData: OHLC[];
  volumeData?: Volume[];
  trades?: Trade[];
  indicators?: Indicator[];
  height?: number;
}

const darkOptions: DeepPartial<ChartOptions> = {
  layout: {
    background: { color: '#0b1220' },
    textColor: '#d1d8e0',
  },
  grid: {
    vertLines: { color: 'rgba(255,255,255,0.03)' },
    horzLines: { color: 'rgba(255,255,255,0.03)' },
  },
  crosshair: {
    mode: CrosshairMode.Normal,
  },
  rightPriceScale: {
    borderColor: 'rgba(255,255,255,0.06)'
  },
  timeScale: {
    borderColor: 'rgba(255,255,255,0.06)'
  }
};

export default function TradingViewChart({ ohlcData, volumeData = [], trades = [], indicators = [], height = 500 }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<any>(null);
  const volumesRef = useRef<any>(null);
  const paneMap = useRef<Record<string, any>>({});
  const resizeObserver = useRef<ResizeObserver | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // create chart
    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: darkOptions.layout,
      grid: darkOptions.grid,
      crosshair: darkOptions.crosshair,
      rightPriceScale: darkOptions.rightPriceScale,
      timeScale: darkOptions.timeScale,
      localization: { dateFormat: 'yyyy-MM-dd' },
    });
    chart.applyOptions({
      layout: darkOptions.layout,
      grid: darkOptions.grid,
    });
    chartRef.current = chart;

    // main candlestick series
    const candleSeries = (chart as any).addSeries({ type: 'Candlestick' } as any, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderVisible: false,
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    } as any);
    seriesRef.current = candleSeries;

    // volume pane (histogram) as a new pane aligned below
    if (volumeData && volumeData.length > 0) {
      const volumePane = (chart as any).addSeries({ type: 'Histogram' } as any, {
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        scaleMargins: { top: 0.8, bottom: 0 },
        color: '#26a69a',
      } as any);
      volumesRef.current = volumePane;
      volumesRef.current.setData(volumeData as any);
    }

    // handle resize
    resizeObserver.current = new ResizeObserver(() => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({ width: containerRef.current.clientWidth, height: containerRef.current.clientHeight });
      chartRef.current.timeScale().fitContent();
    });
    resizeObserver.current.observe(containerRef.current);

    // initial fit
    chart.timeScale().fitContent();

    return () => {
      resizeObserver.current?.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // update data
  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(ohlcData as any);
    chartRef.current?.timeScale().fitContent();

    // markers for trades
    const markers = (trades || []).map((t) => ({
      time: t.time,
      position: t.side === 'buy' ? 'belowBar' : 'aboveBar',
      color: t.side === 'buy' ? '#26a69a' : '#ef5350',
      shape: t.side === 'buy' ? 'arrowUp' : 'arrowDown',
      text: `${t.label || (t.side === 'buy' ? 'Buy' : 'Sell')}\n${t.price}${t.pnl !== undefined ? ` PnL:${t.pnl}` : ''}`,
    }));
    (seriesRef.current as any).setMarkers(markers as any);

    // update volume
    if (volumesRef.current && volumeData) {
      volumesRef.current.setData(volumeData as any);
    }

    // indicators
    if (indicators && indicators.length > 0) {
      indicators.forEach((ind) => {
        const isSeparate = ind.panel === 'separate';
        if (isSeparate) {
          // create a new pane
          const paneSeries = (chartRef.current as any)?.addSeries({ type: 'Line' } as any, { color: ind.color || '#ffcc00', lineWidth: 2 } as any);
          paneSeries?.setData(ind.data as any);
          paneMap.current[ind.name] = paneSeries || null;
        } else {
          // overlay on main price scale
          const overlay = (chartRef.current as any)?.addSeries({ type: 'Line' } as any, { color: ind.color || '#ffcc00', lineWidth: 2 } as any);
          overlay?.setData(ind.data as any);
          paneMap.current[ind.name] = overlay || null;
        }
      });
    }

  }, [ohlcData, volumeData, trades, indicators]);

  // crosshair and mouse interactions are handled by chart options

  return <div ref={containerRef} style={{ width: '100%', height: height, position: 'relative' }} />;
}
