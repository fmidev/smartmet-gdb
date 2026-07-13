/* Self-deadlock: one thread re-locks a non-recursive mutex it already holds.
 * The second lock blocks forever with __owner == the caller's own TID.
 * Validates the SELF-DEADLOCK branch of `deadlock scan`.
 * Build:  cc -g -O0 -pthread deadlock_self.c -o deadlock_self
 */
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

static pthread_mutex_t M = PTHREAD_MUTEX_INITIALIZER;

int main(void)
{
  fprintf(stderr, "deadlock_self pid=%d  M=%p\n", (int)getpid(), (void *)&M);
  fflush(stderr);
  pthread_mutex_lock(&M);
  pthread_mutex_lock(&M); /* self-deadlock */
  return 0;
}
