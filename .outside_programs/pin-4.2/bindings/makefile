#
# Copyright (C) 2024-2024 Intel Corporation.
# SPDX-License-Identifier: MIT
#

AR_SUFFIX := .a

ifndef HOST_ARCH
  HST := $(shell  uname -m)
  ifeq ($(HST),x86)
    HOST_ARCH := ia32
  endif
  ifeq ($(HST),i386)
    HOST_ARCH := ia32
  endif
  ifeq ($(HST),i686)
    HOST_ARCH := ia32
  endif
  ifeq ($(HST),x86_64)
    HOST_ARCH := intel64
  endif
  ifeq ($(HST),amd64)
    HOST_ARCH := intel64
  endif
  # Verify known host
  ifndef HOST_ARCH
    $(error Could not detect the host architecture. Please define HOST_ARCH from the command line.)
  endif
endif

# Define the architecture of the target
TARGET ?= $(HOST_ARCH)
PIP_GLOBAL_ARGS ?=

# Check to see that we have python3 and pip3 installed

export PYTHON3 ?= $(shell which python3)
export PINKIT := $(PINKIT)

.PHONY: all clean cleanall pinglueserver pinglueserver-full pinglueserver-install \
        libpinglue pypinglue test \
			  pinglueserver.test libpinglue.test pypinglue.test pynbin.test \
			  pinglueserver-full.test libpinglue-full.test pypinglue-full.test pynbin-full.test \
			  pinglueserver.setup libpinglue.sertup pypinglue.setup \
			  pinglueserver-full.setup libpinglue-full.setup pypinglue-full.setup

all: pinglueserver libpinglue pypinglue

pinglueserver pinglueserver-full:
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=$(TARGET) pinglueserver.build

pinglueserver-install pinglueserver.setup pinglueserver-full.setup:
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=$(TARGET) install

libpinglue libpinglue.setup libpinglue-full.setup:
	$(MAKE) -C $(PINKIT)/bindings/glue/libpinglue -f makefile TARGET=$(HOST_ARCH)

pypinglue pypinglue.setup pypinglue-full.setup: libpinglue
	$(MAKE) -C $(PINKIT)/bindings/python/pynbin/cymod/pinglue -f makefile TARGET=$(HOST_ARCH) PIP_GLOBAL_ARGS=$(PIP_GLOBAL_ARGS)

clean:
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=ia32 clean
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=intel64 clean
	$(MAKE) -C $(PINKIT)/bindings/glue/libpinglue -f makefile TARGET=$(TARGET) clean
	$(MAKE) -C $(PINKIT)/bindings/python/pynbin/cymod/pinglue -f makefile TARGET=$(TARGET) clean

cleanall:
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=ia32 clean
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=intel64 clean
	$(MAKE) -C $(PINKIT)/bindings/glue/libpinglue -f makefile TARGET=$(TARGET) cleanall
	$(MAKE) -C $(PINKIT)/bindings/python/pynbin/cymod/pinglue -f makefile TARGET=$(TARGET) cleanall

test: pinglueserver-full.test libpinglue-full.test pynbin-full.test

pinglueserver.test: pinglueserver
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=$(TARGET) sanity

libpinglue.test: pinglueserver-install libpinglue
	$(MAKE) -C $(PINKIT)/bindings/glue/libpinglue -f makefile TARGET=$(TARGET) test

pypinglue.test pynbin.test: pinglueserver-install pypinglue
	$(MAKE) -C $(PINKIT)/bindings/python/tests -f makefile TARGET=$(TARGET) sanity

pinglueserver-full.test: pinglueserver
	$(MAKE) -C $(PINKIT)/bindings/glue/pinglueserver -f makefile TARGET=$(TARGET) test

libpinglue-full.test: pinglueserver-install libpinglue
	$(MAKE) -C $(PINKIT)/bindings/glue/libpinglue -f makefile TARGET=$(TARGET) test

pypinglue-full.test pynbin-full.test: pinglueserver-install pypinglue
	$(MAKE) -C $(PINKIT)/bindings/python/tests -f makefile TARGET=$(TARGET) test
