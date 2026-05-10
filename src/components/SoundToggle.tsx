type Props = {
  enabled: boolean;
  onToggle: () => void;
};

export function SoundToggle({ enabled, onToggle }: Props) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={enabled ? '音をオフにする' : '音をオンにする'}
      className={`absolute bottom-4 right-4 z-50 flex min-w-[104px] items-center justify-center gap-2 rounded-full border-4 border-slate-900 px-4 py-2 text-sm font-black shadow-[0_4px_0_#1a1f2c] transition-transform active:translate-y-1 active:shadow-[0_2px_0_#1a1f2c] ${
        enabled ? 'bg-pop-mint text-slate-900' : 'bg-white/90 text-slate-500'
      }`}
    >
      <span className="text-xl leading-none" aria-hidden="true">
        ♪
      </span>
      <span>音 {enabled ? 'ON' : 'OFF'}</span>
    </button>
  );
}
