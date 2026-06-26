'use client';

import { useCallback, useState } from 'react';
import { Download } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

/**
 * Settings card that lets a user download a ZIP of all their data
 * (GDPR portability, #81). Hits GET /api/profile/export — the BFF
 * proxies to the wyrdfold-api's GET /profile/export, which returns a
 * ZIP with `data.json` (every per-user DB row, secrets redacted),
 * `files/` (uploaded resumes + generated documents), and a manifest.
 *
 * The fetch+blob+anchor pattern mirrors the resume-zip export on the
 * jobs list: stream the bytes into a Blob, hand the browser an object
 * URL via a synthetic `<a download>`, then revoke it. `extractApiError`
 * surfaces the upstream `detail` (e.g. a 401 when the session lapsed)
 * instead of a generic failure.
 */
export default function DataExportCard() {
  const { toast } = useToast();
  const [downloading, setDownloading] = useState(false);

  const handleDownload = useCallback(async () => {
    setDownloading(true);
    try {
      const res = await fetch('/api/profile/export');
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Export failed'),
        });
        return;
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'wyrdfold-export.zip';
      a.click();
      URL.revokeObjectURL(url);
      toast({ variant: 'success', title: 'Your data export is downloading' });
    } catch {
      toast({ variant: 'error', title: 'Network error exporting your data' });
    } finally {
      setDownloading(false);
    }
  }, [toast]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Your data</CardTitle>
        <Text variant='meta' className='text-text-secondary'>
          Download a ZIP of everything we hold for your account — profile,
          targets, saved jobs, generated documents, and uploaded resumes. Stored
          API keys are redacted to their provider and last four characters.
        </Text>
      </CardHeader>
      <CardContent>
        <Button
          name='settings-export-data'
          variant='outline'
          size='sm'
          onClick={handleDownload}
          disabled={downloading}
        >
          <Download className='size-4' aria-hidden />
          <span>{downloading ? 'Preparing…' : 'Download my data (.zip)'}</span>
        </Button>
      </CardContent>
    </Card>
  );
}
