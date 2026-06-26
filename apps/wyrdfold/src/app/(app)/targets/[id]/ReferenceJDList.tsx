'use client';

import { useCallback, useState } from 'react';
import { ExternalLink, ThumbsDown, ThumbsUp, Trash2 } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';
import ConfirmModal from '@/components/ConfirmModal';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import type { TargetReferenceJD } from '../types';
import AddReferenceJDModal from './AddReferenceJDModal';

interface ReferenceJDListProps {
  targetId: string;
  referenceJDs: TargetReferenceJD[];
  onChanged: () => void;
}

export default function ReferenceJDList({
  targetId,
  referenceJDs,
  onChanged,
}: ReferenceJDListProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // Reference JD id awaiting delete confirmation; opens the confirm modal.
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  // The caller's own vote per JD (1 up / -1 down / 0 none). Votes are
  // anonymous so the list endpoint never sends the existing vote — we start
  // unknown (0) and reflect the server's echoed `your_vote` after each click.
  const [votes, setVotes] = useState<Record<string, number>>({});
  const [votingId, setVotingId] = useState<string | null>(null);
  const { toast } = useToast();

  const handleDelete = useCallback((refId: string) => {
    setPendingDeleteId(refId);
  }, []);

  const confirmDelete = useCallback(async () => {
    const refId = pendingDeleteId;
    if (!refId) return;

    setDeletingId(refId);
    try {
      const res = await fetch(
        `/api/targets/${targetId}/reference-jds/${refId}`,
        { method: 'DELETE' }
      );
      if (!res.ok) throw new Error(await extractApiError(res, 'Delete failed'));
      toast({ variant: 'success', title: 'Reference JD removed' });
      setPendingDeleteId(null);
      onChanged();
    } catch (err) {
      toast({
        variant: 'error',
        title:
          err instanceof Error ? err.message : 'Failed to delete reference JD',
      });
    } finally {
      setDeletingId(null);
    }
  }, [pendingDeleteId, targetId, toast, onChanged]);

  const handleVote = useCallback(
    async (refId: string, direction: 1 | -1) => {
      // Clicking the active direction again clears the vote (API treats 0 as
      // "remove my vote"); otherwise switch to the chosen direction.
      const value = votes[refId] === direction ? 0 : direction;
      setVotingId(refId);
      try {
        const res = await fetch(
          `/api/targets/${targetId}/reference-jds/${refId}/vote`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value }),
          }
        );
        if (!res.ok) throw new Error(await extractApiError(res, 'Vote failed'));
        const result = (await res.json()) as {
          your_vote: number;
          profile_version: number | null;
        };
        setVotes(prev => ({ ...prev, [refId]: result.your_vote }));
        // A non-null profile_version means this vote flipped suppression and
        // the shared scoring profile was re-merged — refresh so the rest of
        // the detail view reflects it.
        if (result.profile_version !== null) onChanged();
      } catch (err) {
        toast({
          variant: 'error',
          title: err instanceof Error ? err.message : 'Failed to record vote',
        });
      } finally {
        setVotingId(null);
      }
    },
    [votes, targetId, toast, onChanged]
  );

  const handleAdded = useCallback(() => {
    setModalOpen(false);
    onChanged();
  }, [onChanged]);

  return (
    <Card>
      <CardHeader>
        <div className='flex items-center justify-between'>
          <CardTitle>Reference JDs ({referenceJDs.length})</CardTitle>
          <Button
            name='target-ref-jd-add'
            variant='outline'
            size='sm'
            onClick={() => setModalOpen(true)}
          >
            Add Reference JD
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {referenceJDs.length === 0 ? (
          <Text variant='body' as='p' className='text-text-secondary'>
            No reference JDs yet. Add one to automatically derive a scoring
            profile from a job description.
          </Text>
        ) : (
          <div className='flex flex-col gap-3'>
            {referenceJDs.map(jd => (
              <div
                key={jd.id}
                className='flex items-start justify-between gap-3 rounded-lg border border-border p-3'
              >
                <div className='flex flex-col gap-1 min-w-0'>
                  {jd.jd_url && (
                    <a
                      href={jd.jd_url}
                      target='_blank'
                      rel='noopener noreferrer'
                      className='flex items-center gap-1 text-xs text-brand-500 hover:underline'
                    >
                      <ExternalLink className='size-3 shrink-0' aria-hidden />
                      <span className='truncate'>{jd.jd_url}</span>
                    </a>
                  )}
                  <Text variant='caption' as='p' className='line-clamp-2'>
                    {jd.jd_text}
                  </Text>
                  <Text variant='meta' as='span'>
                    Added {new Date(jd.created_at).toLocaleDateString()}
                  </Text>
                </div>
                <div className='flex items-center gap-1 shrink-0'>
                  <Button
                    name={`target-ref-jd-upvote-${jd.id}`}
                    variant='bare'
                    size='sm'
                    iconOnly
                    onClick={() => handleVote(jd.id, 1)}
                    disabled={votingId === jd.id}
                    aria-label='Upvote reference JD'
                    aria-pressed={votes[jd.id] === 1}
                    className={
                      votes[jd.id] === 1
                        ? 'text-success'
                        : 'text-text-tertiary hover:text-success'
                    }
                  >
                    <ThumbsUp className='size-3.5' />
                  </Button>
                  <Button
                    name={`target-ref-jd-downvote-${jd.id}`}
                    variant='bare'
                    size='sm'
                    iconOnly
                    onClick={() => handleVote(jd.id, -1)}
                    disabled={votingId === jd.id}
                    aria-label='Downvote reference JD'
                    aria-pressed={votes[jd.id] === -1}
                    className={
                      votes[jd.id] === -1
                        ? 'text-error'
                        : 'text-text-tertiary hover:text-error'
                    }
                  >
                    <ThumbsDown className='size-3.5' />
                  </Button>
                  <Button
                    name={`target-ref-jd-delete-${jd.id}`}
                    variant='bare'
                    size='sm'
                    iconOnly
                    onClick={() => handleDelete(jd.id)}
                    disabled={deletingId === jd.id}
                    aria-label='Delete reference JD'
                    className='text-text-tertiary hover:text-error'
                  >
                    <Trash2 className='size-3.5' />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      <AddReferenceJDModal
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        targetId={targetId}
        onAdded={handleAdded}
      />

      <ConfirmModal
        isOpen={pendingDeleteId !== null}
        onClose={() => setPendingDeleteId(null)}
        onConfirm={confirmDelete}
        title='Delete reference JD?'
        message='The scoring profile will be re-merged. This cannot be undone.'
        confirmLabel='Delete'
        destructive
        loading={deletingId !== null}
        loadingLabel='Deleting…'
      />
    </Card>
  );
}
