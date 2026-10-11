import { ClerkProvider } from "@clerk/nextjs";
import type { Metadata } from "next";
import { Geist, Geist_Mono, Instrument_Serif } from "next/font/google";

import { Providers } from "@/components/providers";

import "./globals.css";

const sans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const mono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });
const display = Instrument_Serif({
  variable: "--font-display-serif",
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
});

export const metadata: Metadata = {
  title: { default: "Tangible", template: "%s · Tangible" },
  description:
    "Operational due diligence for small-business lenders. The agent investigates, tools compute, policy decides.",
};

const clerk = process.env.NEXT_PUBLIC_AUTH_MODE === "clerk";

export default function RootLayout({ children }: LayoutProps<"/">) {
  const body = <Providers>{children}</Providers>;
  return (
    <html
      lang="en"
      className={`${sans.variable} ${mono.variable} ${display.variable} antialiased`}
      suppressHydrationWarning
    >
      <head>
        {/* Apply a remembered light/dark choice before first paint. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{var t=localStorage.getItem('tangible.theme');if(t==='dark'||t==='light')document.documentElement.dataset.theme=t}catch(e){}",
          }}
        />
      </head>
      <body className="min-h-dvh">
        {clerk ? (
          <ClerkProvider
            appearance={{
              variables: {
                colorPrimary: "#2b5a4c",
                borderRadius: "0.6rem",
                fontFamily: "var(--font-geist-sans)",
              },
            }}
          >
            {body}
          </ClerkProvider>
        ) : (
          body
        )}
      </body>
    </html>
  );
}
