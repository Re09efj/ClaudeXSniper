SHELL=/bin/sh
BENCHMARK=mg
BENCHMARKU=MG

include ../config/make.def

OBJS = mg.o mg_data.o numa_affinity.o ${COMMON}/print_results.o  \
       ${COMMON}/${RAND}.o ${COMMON}/timers.o ${COMMON}/wtime.o

include ../sys/make.common
.SUFFIXES: .c .f .f90 .h .o

${PROGRAM}: config ${OBJS}
	${FLINK} ${FLINKFLAGS} -o ${PROGRAM} ${OBJS} ${F_LIB}

.f90.o:
	${FCOMPILE} $<

.c.o:
	${CC} ${CFLAGS} -c $<

mg.o:		mg.f90 mg_data.o
mg_data.o:      mg_data.f90 npbparams.h
numa_affinity.o:	numa_affinity.c

clean:
	- rm -f *.o *~ *.mod
	- rm -f npbparams.h core
	- if [ -d rii_files ]; then rm -r rii_files; fi
