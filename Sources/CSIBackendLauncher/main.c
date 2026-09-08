#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum stop_mode {
    STOP_NONE = 0,
    STOP_GRACEFUL = 1,
    STOP_FORCE = 2,
};

static volatile sig_atomic_t signal_stop_requested = 0;

static void request_stop(int signal_number) {
    (void)signal_number;
    signal_stop_requested = 1;
}

static pid_t configured_parent(void) {
    const char *value = getenv("CSI_OPENBASE_PARENT_PID");
    if (value == NULL || *value == '\0') {
        return getppid();
    }

    char *end = NULL;
    errno = 0;
    long parsed = strtol(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed <= 1) {
        return -1;
    }
    return (pid_t)parsed;
}

static int parent_is_alive(pid_t original_parent) {
    if (getppid() != original_parent) {
        return 0;
    }
    if (kill(original_parent, 0) == 0) {
        return 1;
    }
    return errno == EPERM;
}

static int child_exit_code(int status) {
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}

static void sleep_quarter_second(void) {
    struct timespec duration = { .tv_sec = 0, .tv_nsec = 250000000 };
    while (nanosleep(&duration, &duration) == -1 && errno == EINTR) {
    }
}

/*
 * Observe child exit without reaping it. Keeping the group leader as a zombie
 * pins both its PID and PGID until every member has been signalled, preventing
 * a recycled identifier from ever becoming a kill target.
 */
static int child_has_exited(pid_t child) {
    siginfo_t information;
    information.si_pid = 0;
    if (waitid(P_PID, (id_t)child, &information, WEXITED | WNOHANG | WNOWAIT) != 0) {
        return errno == ECHILD;
    }
    return information.si_pid == child;
}

static int reap_child(pid_t child) {
    int status = 0;
    while (waitpid(child, &status, 0) == -1) {
        if (errno != EINTR) {
            return 1;
        }
    }
    return child_exit_code(status);
}

static int stop_backend_group(pid_t child, enum stop_mode mode) {
    if (mode != STOP_FORCE) {
        (void)kill(-child, SIGTERM);
        for (int attempt = 0; attempt < 8; attempt++) {
            if (child_has_exited(child)) {
                break;
            }
            sleep_quarter_second();
        }
    }

    /* The unreaped group leader makes this negative-PGID signal reuse-safe. */
    (void)kill(-child, SIGKILL);
    return reap_child(child);
}

static enum stop_mode read_control(pid_t original_parent) {
    if (signal_stop_requested || !parent_is_alive(original_parent)) {
        return STOP_GRACEFUL;
    }

    struct pollfd descriptor = {
        .fd = STDIN_FILENO,
        .events = POLLIN | POLLHUP,
        .revents = 0,
    };
    int result = poll(&descriptor, 1, 250);
    if (result < 0) {
        return errno == EINTR ? STOP_NONE : STOP_GRACEFUL;
    }
    if (result == 0) {
        return STOP_NONE;
    }

    if ((descriptor.revents & POLLIN) != 0) {
        unsigned char command = 0;
        ssize_t count = read(STDIN_FILENO, &command, 1);
        if (count == 1) {
            return command == 'K' ? STOP_FORCE : STOP_GRACEFUL;
        }
        if (count == 0) {
            return STOP_GRACEFUL;
        }
        if (errno != EINTR) {
            return STOP_GRACEFUL;
        }
    }
    if ((descriptor.revents & (POLLHUP | POLLERR | POLLNVAL)) != 0) {
        return STOP_GRACEFUL;
    }
    return STOP_NONE;
}

static void detach_backend_stdin(void) {
    int null_descriptor = open("/dev/null", O_RDONLY);
    if (null_descriptor < 0) {
        _exit(71);
    }
    if (dup2(null_descriptor, STDIN_FILENO) < 0) {
        _exit(71);
    }
    if (null_descriptor != STDIN_FILENO) {
        close(null_descriptor);
    }
}

int main(int argc, char *argv[]) {
    if (argc != 2) {
        fprintf(stderr, "CSIBackendLauncher requires one backend executable path.\n");
        return 64;
    }
    if (access(argv[1], X_OK) != 0) {
        perror("CSIBackendLauncher cannot execute the backend");
        return 66;
    }

    pid_t original_parent = configured_parent();
    if (original_parent <= 1 || original_parent != getppid()) {
        fprintf(stderr, "CSIBackendLauncher parent identity did not match.\n");
        return 65;
    }

    struct sigaction action;
    action.sa_handler = request_stop;
    sigemptyset(&action.sa_mask);
    action.sa_flags = 0;
    if (sigaction(SIGTERM, &action, NULL) != 0
        || sigaction(SIGINT, &action, NULL) != 0) {
        perror("CSIBackendLauncher could not install signal handlers");
        return 71;
    }

    pid_t supervisor_pid = getpid();
    pid_t child = fork();
    if (child == -1) {
        perror("CSIBackendLauncher could not fork");
        return 71;
    }
    if (child == 0) {
        char parent_value[32];
        int written = snprintf(
            parent_value,
            sizeof(parent_value),
            "%ld",
            (long)supervisor_pid
        );
        if (written <= 0 || (size_t)written >= sizeof(parent_value)
            || setenv("CSI_OPENBASE_PARENT_PID", parent_value, 1) != 0) {
            perror("CSIBackendLauncher could not set the backend parent identity");
            _exit(71);
        }
        detach_backend_stdin();
        if (setpgid(0, 0) != 0) {
            perror("CSIBackendLauncher could not create the backend process group");
            _exit(71);
        }
        execl(argv[1], argv[1], (char *)NULL);
        perror("CSIBackendLauncher could not execute the backend");
        _exit(72);
    }

    /* Close the fork/setpgid race from the supervising side as well. */
    if (setpgid(child, child) != 0 && errno != EACCES) {
        perror("CSIBackendLauncher could not assign the backend process group");
        (void)kill(child, SIGKILL);
        return reap_child(child);
    }

    for (;;) {
        if (child_has_exited(child)) {
            return stop_backend_group(child, STOP_GRACEFUL);
        }

        enum stop_mode mode = read_control(original_parent);
        if (mode != STOP_NONE) {
            return stop_backend_group(child, mode);
        }
    }
}
