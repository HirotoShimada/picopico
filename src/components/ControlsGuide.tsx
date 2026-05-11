type Props = {
  /** What each button means in the current mini game. */
  left?: string;
  right?: string;
  confirm?: string;
};

export function ControlsGuide({ left, right, confirm }: Props) {
  return (
    <div className="absolute inset-x-0 bottom-3 z-20 flex justify-center px-4">
      <div className="card flex flex-wrap items-center justify-center gap-6 bg-white/95 px-6 py-3 text-sm font-bold text-slate-800">
        <PlayerCol player={1} left={left} right={right} confirm={confirm} keys={{ left: '←', right: '→', confirm: '↓' }} />
      </div>
    </div>
  );
}

function PlayerCol({
  player,
  left,
  right,
  confirm,
  keys,
}: {
  player: 1 | 2;
  left?: string;
  right?: string;
  confirm?: string;
  keys: { left: string; right: string; confirm: string };
}) {
  const accent = player === 1 ? 'text-p1' : 'text-p2';
  return (
    <div className="flex items-center gap-3">
      <span className={`font-black ${accent}`}>操作</span>
      {left && <KeyHint label={keys.left} desc={left} />}
      {right && <KeyHint label={keys.right} desc={right} />}
      {confirm && <KeyHint label={keys.confirm} desc={confirm} />}
    </div>
  );
}

function KeyHint({ label, desc }: { label: string; desc: string }) {
  return (
    <span className="flex items-center gap-1">
      <kbd className="rounded-md border-2 border-slate-900 bg-pop-yellow px-2 py-0.5 text-xs font-black shadow-[0_2px_0_#1a1f2c]">
        {label}
      </kbd>
      <span className="text-xs text-slate-700">{desc}</span>
    </span>
  );
}
