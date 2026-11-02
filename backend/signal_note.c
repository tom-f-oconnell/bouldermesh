/* Preloaded into the tools the app runs, to record who sends one a fatal signal. Its exit status only
   says which signal it was, and only the process receiving a signal can see who sent it. A sender
   outside the container shows up here as pid 0. SIGKILL can't be caught, so it can't be noted. */
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <unistd.h>

static void note(int sig, siginfo_t *info, void *context) {
    char line[96];
    int n = snprintf(line, sizeof line, "\nsignal_note: signal %d code %d from pid %d uid %u\n",
                     sig, info->si_code, (int)info->si_pid, (unsigned)info->si_uid);
    if (n > 0 && write(STDERR_FILENO, line, (size_t)n) < 0) { /* it's dying anyway */ }
    (void)context;
    signal(sig, SIG_DFL);
    raise(sig);   /* blocked until this returns, and then it ends the process as it would have */
}

__attribute__((constructor)) static void install(void) {
    static const int fatal[] = {SIGTERM, SIGINT, SIGHUP, SIGQUIT};
    struct sigaction action = {0}, before;
    action.sa_sigaction = note;
    action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    for (size_t i = 0; i < sizeof fatal / sizeof *fatal; i++)
        if (sigaction(fatal[i], NULL, &before) == 0 && before.sa_handler == SIG_DFL)   /* not one it was told to ignore */
            sigaction(fatal[i], &action, NULL);
}
