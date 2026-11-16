import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"
import type { ComponentProps } from "react"

const button = cva("btn", {
  variants: { variant: { primary: "btn-primary", outline: "btn-outline", ghost: "btn-ghost", danger: "btn-danger" } },
  defaultVariants: { variant: "outline" },
})

export function Button({ variant, asChild, className, ...props }: ComponentProps<"button"> &
  VariantProps<typeof button> & { asChild?: boolean }) {
  const Component = asChild ? Slot : "button"
  return <Component className={button({ variant, className })} {...props} />
}

