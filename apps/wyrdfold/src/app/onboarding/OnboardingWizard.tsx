'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { ProgressBar } from '@danieljoffe/shared-ui/ProgressBar';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Alert } from '@danieljoffe/shared-ui/Alert';
import WyrdfoldLogo from '@/components/WyrdfoldLogo';
import { completeOnboarding } from './completeOnboarding';
import ConversationChat from '../_components/ConversationChat';
import PathChooser from './PathChooser';
import ResumeUploader from './ResumeUploader';
import IdentityStep from './IdentityStep';
import JobUrlInput, { type JobData } from './JobUrlInput';
import TargetSuggestions from './TargetSuggestions';
import CompletionScreen from './CompletionScreen';

export type OnboardingPath = 'A' | 'B' | 'C';

type Step =
  | 'path-chooser'
  | 'identity'
  | 'upload-resume'
  | 'add-job'
  | 'pick-targets'
  | 'conversation'
  | 'completion';

// ``identity`` runs first thing after path selection in every path.
// Capturing contact name up front prevents the mid-flow window.prompt
// from #683 (Generate Resume / Cover Letter would 400 with "No
// contact name on file" otherwise). Email auto-fills from the
// Supabase auth session; name is the only required field.
const STEPS_BY_PATH: Record<OnboardingPath, Step[]> = {
  A: [
    'path-chooser',
    'identity',
    'upload-resume',
    'add-job',
    'pick-targets',
    'completion',
  ],
  B: [
    'path-chooser',
    'identity',
    'upload-resume',
    'pick-targets',
    'completion',
  ],
  C: ['path-chooser', 'identity', 'conversation', 'pick-targets', 'completion'],
};

export default function OnboardingWizard() {
  const router = useRouter();
  const [currentStep, setCurrentStep] = useState<Step>('path-chooser');
  const [selectedPath, setSelectedPath] = useState<OnboardingPath | null>(null);
  const [jobData, setJobData] = useState<JobData | null>(null);
  const [skipping, setSkipping] = useState(false);
  const [skipFailed, setSkipFailed] = useState(false);
  const stepRef = useRef<HTMLDivElement>(null);

  const steps = selectedPath ? STEPS_BY_PATH[selectedPath] : ['path-chooser'];
  const stepIndex = steps.indexOf(currentStep);
  const totalSteps = steps.length;

  // Move focus to the new step content on transition
  useEffect(() => {
    stepRef.current?.focus();
  }, [currentStep]);

  const goNext = useCallback(() => {
    if (!selectedPath) return;
    const stepsForPath = STEPS_BY_PATH[selectedPath];
    const idx = stepsForPath.indexOf(currentStep);
    if (idx < stepsForPath.length - 1) {
      setCurrentStep(stepsForPath[idx + 1]);
    }
  }, [selectedPath, currentStep]);

  const handlePathSelect = useCallback((path: OnboardingPath) => {
    setSelectedPath(path);
    const firstStep = STEPS_BY_PATH[path][1];
    setCurrentStep(firstStep);
  }, []);

  const handleSkip = useCallback(async () => {
    // Mark onboarding complete on skip so the user isn't bounced back to
    // /onboarding by the dashboard's completed_at gate. CompletionScreen
    // hits the same endpoint on the "happy path" finish; the API is
    // idempotent (complete_onboarding short-circuits if completed_at is
    // already set) so re-completing after a finish is a no-op.
    //
    // We MUST confirm the write landed (HTTP 2xx) before navigating.
    // ``completeOnboarding`` checks ``res.ok`` — a non-2xx (expired
    // session → 401, API down → 503) used to be swallowed, navigating
    // away while ``onboarding_completed_at`` stayed NULL so the next
    // dashboard visit re-fired the wizard (the "skip doesn't stick" bug).
    // On a confirmed failure we keep the user here with a retry
    // affordance instead of dropping them into that redirect loop.
    setSkipping(true);
    setSkipFailed(false);
    const ok = await completeOnboarding();
    if (!ok) {
      setSkipping(false);
      setSkipFailed(true);
      return;
    }
    router.push('/targets');
  }, [router]);

  return (
    <div className='flex min-h-screen items-center justify-center bg-bg px-4 py-12'>
      <div className='w-full max-w-2xl'>
        {/* Header */}
        <div className='mb-8 flex flex-col items-center text-center'>
          {currentStep === 'path-chooser' && (
            <WyrdfoldLogo aria-label='WyrdFold' className='mb-4 h-12 w-16' />
          )}
          <Heading variant='hero' as='h1'>
            Welcome to WyrdFold
          </Heading>
          <Text variant='body' className='mt-2 text-text-secondary'>
            {currentStep === 'path-chooser'
              ? 'How would you like to get started?'
              : `Step ${stepIndex + 1} of ${totalSteps}`}
          </Text>
          {currentStep === 'path-chooser' && (
            <Text
              variant='caption'
              className='mt-3 max-w-md text-text-tertiary'
            >
              Match scores start rough and get more accurate as you add resume
              content, target roles, and preferences.
            </Text>
          )}
        </div>

        {/* Progress bar */}
        {selectedPath && currentStep !== 'completion' && (
          <div className='mb-8'>
            <ProgressBar
              value={stepIndex + 1}
              max={totalSteps}
              size='sm'
              aria-label={`Step ${stepIndex + 1} of ${totalSteps}`}
            />
          </div>
        )}

        {/* Skip failed to persist server-side — surface a retry rather
            than navigating into the dashboard's redirect loop. */}
        {skipFailed && (
          <div className='mb-4'>
            <Alert variant='error'>
              We couldn&apos;t save your progress. Check your connection and{' '}
              <button
                type='button'
                onClick={handleSkip}
                disabled={skipping}
                className='font-medium underline underline-offset-2 disabled:opacity-60'
              >
                try again
              </button>
              .
            </Alert>
          </div>
        )}

        {/* Step content — tabIndex={-1} allows programmatic focus */}
        <div
          ref={stepRef}
          tabIndex={-1}
          aria-label={`Onboarding step: ${currentStep.replace(/-/g, ' ')}`}
          className='outline-none'
        >
          {currentStep === 'path-chooser' && (
            <PathChooser onSelect={handlePathSelect} onSkip={handleSkip} />
          )}
          {currentStep === 'identity' && (
            <IdentityStep onComplete={goNext} onSkip={handleSkip} />
          )}
          {currentStep === 'upload-resume' && (
            <ResumeUploader onComplete={goNext} onSkip={handleSkip} />
          )}
          {currentStep === 'add-job' && (
            <JobUrlInput
              onComplete={data => {
                if (data) setJobData(data);
                goNext();
              }}
              onSkip={handleSkip}
            />
          )}
          {currentStep === 'pick-targets' && (
            <TargetSuggestions
              onComplete={goNext}
              onSkip={handleSkip}
              jobData={jobData}
            />
          )}
          {currentStep === 'conversation' && (
            <ConversationChat onComplete={goNext} onSkip={handleSkip} />
          )}
          {currentStep === 'completion' && <CompletionScreen />}
        </div>
      </div>
    </div>
  );
}
