"use client";

import * as React from "react";
import { SWRConfig, useSWRConfig, type SWRConfiguration } from "swr";

import { Toaster } from "@/components/ui/toast";
import { TooltipProvider } from "@/components/ui/tooltip";
import { isApiError, subscribeApiToken, swrFetcher } from "@/lib/api";

const MAX_RETRIES = 3;

const swrConfig: SWRConfiguration = {
  fetcher: swrFetcher,
  dedupingInterval: 2_000,
  focusThrottleInterval: 5_000,
  onErrorRetry(error, _key, _config, revalidate, { retryCount }) {
    // Client errors (404, 401, 422 …) will not fix themselves.
    if (isApiError(error) && error.status >= 400 && error.status < 500) return;
    if (retryCount >= MAX_RETRIES) return;
    const delay = Math.min(15_000, 1_500 * 2 ** retryCount);
    setTimeout(() => revalidate({ retryCount }), delay);
  },
};

/** Revalidates every SWR key when the API token changes (e.g. after saving it on /settings). */
function RevalidateOnTokenChange() {
  const { mutate } = useSWRConfig();
  React.useEffect(() => subscribeApiToken(() => void mutate(() => true)), [mutate]);
  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig value={swrConfig}>
      <TooltipProvider>
        <RevalidateOnTokenChange />
        {children}
        <Toaster />
      </TooltipProvider>
    </SWRConfig>
  );
}
