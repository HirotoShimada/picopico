import { useMemo } from 'react';
import type { CSSProperties } from 'react';

const COLORS = ['#ffd23f', '#ff5fa2', '#5ee2c1', '#3aa3ff', '#9b6dff', '#ffffff'];

type Props = {
  active?: boolean;
  pieces?: number;
  className?: string;
};

export function ConfettiBurst({ active = true, pieces = 72, className = '' }: Props) {
  const confetti = useMemo(
    () =>
      Array.from({ length: pieces }, (_, i) => {
        const angle = Math.random() * Math.PI * 2;
        const distance = 150 + Math.random() * 520;
        return {
          id: i,
          x: 42 + Math.random() * 16,
          y: 38 + Math.random() * 18,
          dx: Math.cos(angle) * distance,
          dy: Math.sin(angle) * distance + 160,
          rotate: Math.random() * 900 - 450,
          delay: Math.random() * 180,
          duration: 780 + Math.random() * 580,
          width: 8 + Math.random() * 9,
          height: 12 + Math.random() * 16,
          color: COLORS[i % COLORS.length],
          round: Math.random() > 0.55,
        };
      }),
    [pieces],
  );

  if (!active) return null;

  return (
    <div className={`pointer-events-none absolute inset-0 z-40 overflow-hidden ${className}`} aria-hidden="true">
      {confetti.map((piece) => (
        <i
          key={piece.id}
          className="confetti-piece"
          style={
            {
              left: `${piece.x}%`,
              top: `${piece.y}%`,
              width: piece.width,
              height: piece.height,
              backgroundColor: piece.color,
              borderRadius: piece.round ? '999px' : '3px',
              '--dx': `${piece.dx}px`,
              '--dy': `${piece.dy}px`,
              '--rot': `${piece.rotate}deg`,
              '--delay': `${piece.delay}ms`,
              '--dur': `${piece.duration}ms`,
            } as CSSProperties
          }
        />
      ))}
    </div>
  );
}
