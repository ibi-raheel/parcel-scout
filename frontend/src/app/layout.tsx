import type { Metadata } from "next";
import "./globals.css";
import { QueryProvider } from "@/lib/QueryProvider";

export const metadata: Metadata = {
  title: "Parcel Scout v3",
  description:
    "Real estate intelligence platform — distressed property discovery",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full flex flex-col bg-slate-900 text-slate-200 antialiased">
        <QueryProvider>{children}</QueryProvider>
      </body>
    </html>
  );
}
