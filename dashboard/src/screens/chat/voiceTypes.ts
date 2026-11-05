import type { ComponentType } from "react";

/** Contract of `./voice/VoiceRecorder` (built by the voice delivery). It renders the microphone button of the design. */
export interface VoiceRecorderProps {
  /** The recording was transcribed: text for the message and the id of the stored audio attachment. */
  onTranscribed: (text: string, attachmentId: string) => void;
  /** Recording or transcription failed. `message` is user-safe text when the component has one. */
  onError: (message?: string) => void;
  disabled?: boolean;
}

const found = import.meta.glob<{ VoiceRecorder: ComponentType<VoiceRecorderProps> }>("./voice/VoiceRecorder.tsx", { eager: true });

/** The recorder component, or null while the voice delivery is not in the tree (the button is then not drawn). Tests replace it. */
export const voice: { Recorder: ComponentType<VoiceRecorderProps> | null } = {
  Recorder: Object.values(found)[0]?.VoiceRecorder ?? null,
};
