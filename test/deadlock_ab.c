/* Synthetic AB-BA mutex deadlock to validate deadlock.py.
 *
 *   thread "worker-A" : lock(A) -> lock(B)   (blocks on B)
 *   thread "worker-B" : lock(B) -> lock(A)   (blocks on A)
 *
 * Both threads park forever in pthread_mutex_lock -> futex, forming a
 * two-node wait-for cycle.  Build:  cc -g -O0 -pthread deadlock_ab.c -o deadlock_ab
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

static pthread_mutex_t A = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t B = PTHREAD_MUTEX_INITIALIZER;

static void *worker_a(void *arg)
{
  (void)arg;
  pthread_setname_np(pthread_self(), "worker-A");
  pthread_mutex_lock(&A);
  sleep(1); /* let worker-B grab B first */
  pthread_mutex_lock(&B); /* deadlock: B held by worker-B */
  pthread_mutex_unlock(&B);
  pthread_mutex_unlock(&A);
  return NULL;
}

static void *worker_b(void *arg)
{
  (void)arg;
  pthread_setname_np(pthread_self(), "worker-B");
  pthread_mutex_lock(&B);
  sleep(1); /* let worker-A grab A first */
  pthread_mutex_lock(&A); /* deadlock: A held by worker-A */
  pthread_mutex_unlock(&A);
  pthread_mutex_unlock(&B);
  return NULL;
}

int main(void)
{
  pthread_t ta, tb;
  fprintf(stderr, "deadlock_ab pid=%d  A=%p B=%p\n", (int)getpid(),
          (void *)&A, (void *)&B);
  fflush(stderr);
  pthread_create(&ta, NULL, worker_a, NULL);
  pthread_create(&tb, NULL, worker_b, NULL);
  pthread_join(ta, NULL);
  pthread_join(tb, NULL);
  return 0;
}
