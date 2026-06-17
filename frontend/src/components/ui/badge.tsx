import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-medium',
  {
    variants: {
      variant: {
        default: 'border-[var(--color-border)] bg-[var(--color-accent-soft)] text-[var(--color-text)]',
        success: 'border-green-500/20 bg-green-500/10 text-[var(--color-success)]',
        warning: 'border-amber-500/20 bg-amber-500/10 text-[var(--color-warning)]',
        danger: 'border-red-500/20 bg-red-500/10 text-[var(--color-danger)]',
        secondary: 'border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text-secondary)]',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
