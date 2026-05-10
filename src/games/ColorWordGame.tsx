import { useMemo, useRef, useState } from 'react';
import type { MiniGameProps, PlayerId } from '../types';
import { useCountdown } from '../hooks/useCountdown';
import { useFreshInput } from '../hooks/useFreshInput';
import { ControlsGuide } from '../components/ControlsGuide';

type Choice = 'red' | 'blue' | 'yellow';

const CHOICES: { id: Choice; label: string; bg: string; key: 'left' | 'confirm' | 'right' }[] = [
  { id: 'red', label: 'あか', bg: 'bg-rose-500', key: 'left' },
  { id: 'blue', label: 'あお', bg: 'bg-sky-500', key: 'confirm' },
  { id: 'yellow', label: 'きいろ', bg: 'bg-amber-400', key: 'right' },
];

const KEY_LABELS: Record<Choice, { p1: string; p2: string }> = {
  red: { p1: 'A', p2: '←' },
  blue: { p1: 'Space', p2: 'Enter' },
  yellow: { p1: 'D', p2: '→' },
};

export function ColorWordGame({ durationMs, input, onFinish }: MiniGameProps) {
  // Stroop-style: word and ink color often disagree.
  const correct = useMemo(() => CHOICES[Math.floor(Math.random() * CHOICES.length)], []);
  const inkColor = useMemo(() => {
    // 70% chance the ink mismatches.
    if (Math.random() < 0.3) return correct;
    const others = CHOICES.filter((c) => c.id !== correct.id);
    return others[Math.floor(Math.random() * others.length)];
  }, [correct]);

  const [answers, setAnswers] = useState<Record<PlayerId, Choice | null>>({ 1: null, 2: null });
  const finishedRef = useRef(false);

  useCountdown(durationMs, () => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    finalize(answers);
  });

  useFreshInput(input, (e) => {
    if (finishedRef.current) return;
    const choice = CHOICES.find((c) => c.key === e.action);
    if (!choice) return;
    if (answers[e.player] != null) return;
    const next = { ...answers, [e.player]: choice.id } as Record<PlayerId, Choice | null>;
    setAnswers(next);
    if (next[1] && next[2]) {
      finishedRef.current = true;
      window.setTimeout(() => finalize(next), 250);
    }
  });

  function finalize(state: Record<PlayerId, Choice | null>) {
    const winners: PlayerId[] = [];
    const labels: Partial<Record<PlayerId, string>> = {};
    ([1, 2] as PlayerId[]).forEach((p) => {
      if (state[p] === correct.id) {
        winners.push(p);
        labels[p] = '正解！';
      } else if (state[p] == null) {
        labels[p] = '時間切れ';
      } else {
        labels[p] = '不正解';
      }
    });
    onFinish({ winners, labels });
  }

  // Indicate which on-screen color goes with which key.
  const keyHints = { left: 'あか', confirm: 'あお', right: 'きいろ' } as const;

  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="flex flex-col items-center gap-10">
        <div className="text-3xl font-black text-slate-800">この「言葉」のいろを選べ！</div>
        <div className={`card flex h-44 w-[440px] items-center justify-center bg-white text-7xl font-black ${textColor(inkColor.id)}`}>
          {correct.label}
        </div>
        <div className="grid grid-cols-3 gap-4">
          {CHOICES.map((c) => (
            <ColorChoiceCard
              key={c.id}
              choice={c}
              selectedBy={([1, 2] as PlayerId[]).filter((p) => answers[p] === c.id)}
            />
          ))}
        </div>
        <div className="grid grid-cols-2 gap-4 text-center">
          {([1, 2] as PlayerId[]).map((p) => (
            <div key={p} className={`card px-5 py-3 ${p === 1 ? 'bg-p1' : 'bg-p2'} text-white min-w-[180px]`}>
              <div className="text-xs font-extrabold tracking-widest">{p === 1 ? '1P' : '2P'}</div>
              <div className="text-2xl font-black">{answers[p] ? CHOICES.find((c) => c.id === answers[p])!.label : '...'}</div>
            </div>
          ))}
        </div>
      </div>
      <ControlsGuide left={keyHints.left} confirm={keyHints.confirm} right={keyHints.right} />
    </div>
  );
}

function textColor(id: Choice) {
  switch (id) {
    case 'red':
      return 'text-rose-500';
    case 'blue':
      return 'text-sky-500';
    case 'yellow':
      return 'text-amber-400';
  }
}

function keyForChoice(id: Choice) {
  return KEY_LABELS[id];
}

function ColorChoiceCard({
  choice,
  selectedBy,
}: {
  choice: (typeof CHOICES)[number];
  selectedBy: PlayerId[];
}) {
  const keys = keyForChoice(choice.id);
  return (
    <div className="flex flex-col items-center gap-2">
      <div
        className={`card relative flex h-28 w-36 items-center justify-center ${choice.bg} text-3xl font-black text-white transition-transform ${
          selectedBy.length > 0 ? 'scale-105' : ''
        }`}
      >
        <div>{choice.label}</div>
        {selectedBy.length > 0 && (
          <div className="absolute -top-4 flex gap-1">
            {selectedBy.map((p) => (
              <span
                key={p}
                className={`rounded-full border-2 border-slate-900 px-2 py-0.5 text-xs font-black shadow-[0_2px_0_#1a1f2c] ${
                  p === 1 ? 'bg-p1' : 'bg-p2'
                }`}
              >
                {p}P
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1 rounded-full border-2 border-slate-900 bg-white px-2 py-1 text-xs font-black shadow-[0_2px_0_#1a1f2c]">
        <span className="rounded bg-p1 px-1.5 py-0.5 text-white">1P {keys.p1}</span>
        <span className="rounded bg-p2 px-1.5 py-0.5 text-white">2P {keys.p2}</span>
      </div>
    </div>
  );
}
