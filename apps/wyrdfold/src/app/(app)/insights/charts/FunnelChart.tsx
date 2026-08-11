'use client';

import Link from 'next/link';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { JobStatus } from '../../jobs/types';
import type { FunnelStage } from '../types';
import { ChartFigure, type ChartColumn } from './ChartFigure';
import { CHART_AXIS_TICK, CHART_COLORS, STATUS_CHART_COLOR } from './colors';

const DRILL_LINK_CLASS =
  'inline-flex items-center rounded-full bg-surface-tertiary px-2 py-0.5 text-xs text-text-primary tabular-nums hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-1';

interface FunnelChartProps {
  data: FunnelStage[];
}

const STAGE_LABELS: Record<string, string> = {
  saved: 'Saved',
  resume_draft: 'Draft',
  resume_ready: 'Ready',
  applied: 'Applied',
  interviewing: 'Interview',
  offer: 'Offer',
};

// `new` is the default state on creation, not a real funnel step —
// filter it so the chart reflects intentional progression only.
const HIDDEN_STAGES = new Set(['new']);

function formatLabel(slug: string): string {
  return slug
    .split(/[_\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(' ');
}

function stageFill(stage: string): string {
  return STATUS_CHART_COLOR[stage as JobStatus] ?? CHART_COLORS.muted;
}

export default function FunnelChart({ data }: FunnelChartProps) {
  const visible = data.filter(d => !HIDDEN_STAGES.has(d.stage));

  if (visible.length === 0 || visible.every(d => d.count === 0)) {
    return (
      <p className='text-sm text-text-secondary py-8 text-center'>
        Save jobs and update their status as you progress to see your funnel
        build out.
      </p>
    );
  }

  const formatted = visible.map(d => ({
    ...d,
    label: STAGE_LABELS[d.stage] ?? formatLabel(d.stage),
  }));

  const columns: ChartColumn<(typeof formatted)[number]>[] = [
    { header: 'Stage', render: row => row.label },
    { header: 'Jobs', render: row => row.count },
  ];

  const drillStages = formatted.filter(row => row.count > 0);

  return (
    <>
      <ChartFigure
        ariaLabel='Pipeline funnel: jobs by stage'
        rows={formatted}
        columns={columns}
        rowKey={row => row.stage}
      >
        <ResponsiveContainer width='100%' height={250}>
          <BarChart data={formatted} layout='vertical'>
            <CartesianGrid
              strokeDasharray='3 3'
              stroke={CHART_COLORS.grid}
              horizontal={false}
            />
            <XAxis type='number' allowDecimals={false} tick={CHART_AXIS_TICK} />
            <YAxis
              type='category'
              dataKey='label'
              width={70}
              tick={CHART_AXIS_TICK}
            />
            <Tooltip contentStyle={{ fontSize: 12 }} />
            <Bar dataKey='count' name='Jobs' radius={[0, 4, 4, 0]}>
              {formatted.map(entry => (
                <Cell
                  key={entry.stage}
                  fill={stageFill(entry.stage)}
                  fillOpacity={0.85}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </ChartFigure>
      {drillStages.length > 0 && (
        <nav
          aria-label='View jobs by stage'
          className='mt-3 flex flex-wrap items-center gap-1.5'
        >
          <span className='text-xs text-text-tertiary'>View jobs:</span>
          {drillStages.map(row => (
            // No count on the chip, deliberately (2026-08-08). The funnel
            // spans EVERY target the user has (`_user_target_ids` is
            // any-status), while /jobs?status= shows only ACTIVE targets — so
            // with any paused target the chip advertised a number its own
            // destination could not produce. Prod: 6 targets, 1 active, 5 jobs
            // saved, 2 reachable in the list; the chip said "Saved (3)" and
            // landed on "No jobs found". The bar directly above already carries
            // the number, so dropping it here loses nothing and stops the chip
            // making a promise the list can't keep.
            <Link
              key={row.stage}
              href={`/jobs?status=${row.stage}`}
              prefetch={false}
              className={DRILL_LINK_CLASS}
            >
              {row.label}
            </Link>
          ))}
        </nav>
      )}
    </>
  );
}
