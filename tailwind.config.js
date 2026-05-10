/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        p1: '#ff5577',
        p2: '#3aa3ff',
        pop: {
          yellow: '#ffd23f',
          pink: '#ff5fa2',
          mint: '#5ee2c1',
          violet: '#9b6dff',
        },
      },
      fontFamily: {
        display: ['"Hiragino Maru Gothic ProN"', '"Yu Gothic UI"', '"Segoe UI"', 'system-ui', 'sans-serif'],
      },
      keyframes: {
        wiggle: {
          '0%, 100%': { transform: 'rotate(-3deg) scale(1)' },
          '50%': { transform: 'rotate(3deg) scale(1.05)' },
        },
        popin: {
          '0%': { transform: 'scale(0.4)', opacity: '0' },
          '70%': { transform: 'scale(1.15)', opacity: '1' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
        flashbg: {
          '0%, 100%': { backgroundColor: 'rgba(255,255,255,0)' },
          '50%': { backgroundColor: 'rgba(255,255,255,0.4)' },
        },
        floatup: {
          '0%': { transform: 'translateY(20px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        shake: {
          '0%, 100%': { transform: 'translateX(0)' },
          '20%': { transform: 'translateX(-8px)' },
          '40%': { transform: 'translateX(8px)' },
          '60%': { transform: 'translateX(-6px)' },
          '80%': { transform: 'translateX(6px)' },
        },
      },
      animation: {
        wiggle: 'wiggle 600ms ease-in-out infinite',
        popin: 'popin 300ms cubic-bezier(0.16, 1, 0.3, 1)',
        flashbg: 'flashbg 250ms ease-out 1',
        floatup: 'floatup 300ms ease-out',
        shake: 'shake 400ms ease-in-out',
      },
    },
  },
  plugins: [],
};
