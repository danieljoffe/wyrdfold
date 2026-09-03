'use client';

import { Modal } from '@danieljoffe/shared-ui/Modal';
import ConversationChat from './ConversationChat';

interface ConversationChatModalProps {
  isOpen: boolean;
  onClose: () => void;
  onComplete?: () => void;
}

export default function ConversationChatModal({
  isOpen,
  onClose,
  onComplete,
}: ConversationChatModalProps) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      size='lg'
      title='Fill in profile details'
    >
      <ConversationChat
        onComplete={() => {
          onComplete?.();
          onClose();
        }}
        onSkip={onClose}
        /* #844 §3: this modal opens from an ESTABLISHED profile (the Gaps
           card), where the wizard's "Build my profile" / "Skip for now"
           read like the first-run flow reused without relabelling. */
        skipLabel='Close'
        finishLabel='Update my profile'
        finishingLabel='Updating...'
      />
    </Modal>
  );
}
