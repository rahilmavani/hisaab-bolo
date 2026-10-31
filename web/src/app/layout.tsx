import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, Mukta } from "next/font/google";
import "./globals.css";

// Mukta (Ek Type) covers Latin, Devanagari and the ₹ sign, so Hinglish transcripts
// and Hindi replies render in the same face as the rest of the UI.
const mukta = Mukta({
  variable: "--font-mukta",
  subsets: ["latin", "devanagari"],
  weight: ["400", "500", "600", "700"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Hisaab Bolo",
  description: "Your Paytm payments, explained. Just ask.",
};

export const viewport: Viewport = {
  themeColor: "#f7f4ee",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${mukta.variable} ${plexMono.variable} antialiased`}>
      <body>{children}</body>
    </html>
  );
}
