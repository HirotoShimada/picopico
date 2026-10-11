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

/**
 * Drops every cached response and refetches when the API token changes (e.g. after saving it on
 * /settings): data of the previous user must not stay on screen if the new one may not see it.
 */
function RevalidateOnTokenChange() {
  const { mutate } = useSWRConfig();
  React.useEffect(() => subscribeApiToken(() => void mutate(() => true, undefined, { revalidate: true })), [mutate]);
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
