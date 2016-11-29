#
# Specfile for the dCache SmallFiles service
#
Summary: Scripts that allow handling of small files with dCache
Name: dcache-smallfiles-pool
Version: 1.1
Release: 1
License: GPL
Group: Applications/Services
Source: https://github.com/dCache/SmallFiles/archive/master/master.zip
URL: https://github.com/dCache/SmallFiles.git
BuildRoot: %{_tmppath}/%{name}-root
BuildArch: noarch
Distribution: dCache
Prefix: %{_prefix}
Vendor: dCache.org
Packager: dCache <support@dcache.org>
Requires: dcap-libs, 

%description
dcache-smallfiles is a collection of scripts running as a service
to transparently combine bunches of small files in dCache into
large container files that are more suitable to be written to tape
systems.
   
%prep
rm -rf $RPM_BUILD_ROOT/*
rm -rf $RPM_BUILD_DIR/*
wget -O $RPM_SOURCE_DIR/master.zip https://github.com/dCache/SmallFiles/archive/master/master.zip
unzip $RPM_SOURCE_DIR/master.zip
mv SmallFiles*/* .

%build

%install
install --directory ${RPM_BUILD_ROOT}/usr/share/dcache/lib
install --mode 755 $RPM_BUILD_DIR/src/skel/usr/share/dcache/lib/hsm-internal.sh ${RPM_BUILD_ROOT}/usr/share/dcache/lib/hsm-internal.sh
install --mode 755 $RPM_BUILD_DIR/src/skel/usr/share/dcache/lib/datasetPut.js ${RPM_BUILD_ROOT}/usr/share/dcache/lib/datasetPut.js

%files
/usr/share/dcache/lib/hsm-internal.sh
/usr/share/dcache/lib/datasetPut.js

